#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys, os

sys.path.append(".")
from explab import *

from nmtlab import Vocab, MTDataset
from nmt_dataset import NMTDataset
from nnlab import nn
import nnlab.nn.functional as F
import tensorflow as tf
import time
import horovod.tensorflow as hvd
from tensorflow.python.framework.errors_impl import DeadlineExceededError



timeout_option = tf.RunOptions(timeout_in_ms=1000)
# Initialize Horovod
hvd.init()


vocab_path_de = "{}/iwslt14.tokenized.de-en/iwslt14.de.bpe20k.vocab".format(MAINLINE_ROOT)
vocab_de = Vocab(vocab_path_de)
vocab_path_en = "{}/iwslt14.tokenized.de-en/iwslt14.en.bpe20k.vocab".format(MAINLINE_ROOT)
vocab_en = Vocab(vocab_path_en)

text_path_de = "{}/iwslt14.tokenized.de-en/train.de.bpe20k".format(MAINLINE_ROOT)
text_path_en = "{}/iwslt14.tokenized.de-en/train.en.bpe20k".format(MAINLINE_ROOT)
valid_text_path_de = "{}/iwslt14.tokenized.de-en/valid.de.bpe20k".format(MAINLINE_ROOT)
valid_text_path_en = "{}/iwslt14.tokenized.de-en/valid.en.bpe20k".format(MAINLINE_ROOT)


print("hvd local_rank={} size={}".format(hvd.local_rank(), hvd.size()))
d = NMTDataset(text_path_de, text_path_en, vocab_path_de, vocab_path_en, gpu_id=hvd.local_rank(), gpu_num=hvd.size())
valid_d = NMTDataset(valid_text_path_de, valid_text_path_en, vocab_path_de, vocab_path_en, max_lines=32 * 50)

# Define
src_emb_layer = nn.Embedding(vocab_de.size(), 256)
tgt_emb_layer = nn.Embedding(vocab_en.size(), 256)
with tf.variable_scope("enc"):
    fw_cell = tf.nn.rnn_cell.BasicLSTMCell(256)
    bw_cell = tf.nn.rnn_cell.BasicLSTMCell(256)
with tf.variable_scope("dec"):
    decoder_cell = tf.nn.rnn_cell.BasicLSTMCell(256)
expand_layer = nn.Dense(256, vocab_en.size())

# dense1 = nn.Dense(256 * 2, 256)
# dense2 = nn.Dense(256 * 2, 256)
# dense3 = nn.Dense(256 * 2, 256)
# dense4 = nn.Dense(256, 1)
#
# config = tf.ConfigProto()
# config.gpu_options.visible_device_list = str(hvd.local_rank())
# config.allow_soft_placement = True
# ss = tf.Session(config=config)
# ss.run(tf.global_variables_initializer())
# d.initialize(ss)
# valid_d.initialize(ss)
# d.start_epoch()
# import pdb;pdb.set_trace()


# Run >>>
def build_graph(src, tgt):
    global decoder_cell
    src_len = (tf.reduce_sum(tf.to_int32(src > 0), axis=1))
    tgt_len = (tf.reduce_sum(tf.to_int32(tgt > 0), axis=1))
    inp_emb = src_emb_layer.get(F.tensor_from_tf(src)).tf
    tgt_emb = tgt_emb_layer.get(F.tensor_from_tf(tgt)).tf
    
    (fw_states, bw_states), _ = tf.nn.bidirectional_dynamic_rnn(
        fw_cell, bw_cell, inp_emb,
        sequence_length=src_len, swap_memory=True, dtype=tf.float32, scope="enc")

    # enc_states = tf.concat([fw_states, bw_states], axis=2)
    enc_states = fw_states
    attention_states = tf.transpose(enc_states, [1, 0, 2])
    attention_mechanism = tf.contrib.seq2seq.LuongAttention(
        256, attention_states,
        memory_sequence_length=src_len)
    decoder_cell = tf.contrib.seq2seq.AttentionWrapper(
        decoder_cell,
        attention_mechanism,
        attention_layer_size=256)

    # Helper
    helper = tf.contrib.seq2seq.TrainingHelper(
        tgt_emb, tgt_len,
        time_major=False)
    
    # Decoder
    my_decoder = tf.contrib.seq2seq.BasicDecoder(
        decoder_cell,
        helper,
        decoder_cell.zero_state(batch_size=32, dtype=tf.float32))

    # Dynamic decoding
    outputs, final_context_state, _ = tf.contrib.seq2seq.dynamic_decode(
        my_decoder,
        output_time_major=False,
        swap_memory=True,
        scope="dec")
    
    output = expand_layer.forward(F.tensor_from_tf(outputs.rnn_output)).tf
    crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
        labels=tgt, logits=output)
    loss = (tf.reduce_sum(crossent * tf.to_float(tgt > 0)) /
                  32)
    
    return loss, tf.constant(0.5)

loss, acc = build_graph(*d.get_iterator(batch_size=32))
valid_loss, valid_acc = build_graph(*valid_d.get_iterator(batch_size=32))
opt = tf.train.AdamOptimizer(0.0001)
opt = hvd.DistributedOptimizer(opt)
gradients, variables = zip(*opt.compute_gradients(loss))
gradients, _ = tf.clip_by_global_norm(gradients, 3)
train_op = opt.apply_gradients(zip(gradients, variables))

# Pin GPU to be used to process local rank (one GPU per process)
config = tf.ConfigProto()
config.gpu_options.visible_device_list = str(hvd.local_rank())
config.allow_soft_placement = True
my_rank = hvd.local_rank()

ss = tf.Session(config=config)

ss.run(tf.global_variables_initializer())
d.initialize(ss)
valid_d.initialize(ss)

# d.start_epoch()
# import pdb;pdb.set_trace()

ss.run(hvd.broadcast_global_variables(0))

best_loss = 100000
best_counter = 0
done = False

curr_lr = 0.0001

saver = tf.train.Saver()
for epoch in range(10):
    
    count = 0
    start_time = time.time()
    tot_loss = 0.0
    tot_acc = 0.0
    d.start_epoch(n_threads=2)
    while not d.epoch_ended():
        count += 1
        
        try:
            loss1, acc1, qsz1, _= ss.run([
                loss, acc, d._size_op, train_op
            ], options=timeout_option)
        except DeadlineExceededError:
            continue
        
        # acc = 0
        tot_loss += loss1
        tot_acc += acc1
        sys.stdout.write('[{}] loss={:.3f}, acc={:.2f}, qsz={}, bps={:.1f} prog={:.1f}%            \r'.format(
            my_rank,
            loss1, acc1, qsz1, count / (time.time() - start_time), d.progress() * 100
        ))
        sys.stdout.flush()
        
        if count % 100 == 0:
            ss.run(hvd.broadcast_global_variables(0))
        
        if count % 1500 == 0 and my_rank == 0:
            
            valid_loss1 = 0.
            valid_acc1 = 0.
            valid_cnt1 = 0
            valid_d.start_epoch()
            while not valid_d.epoch_ended():
                try:
                    loss1, acc1 = ss.run([
                        valid_loss, valid_acc
                    ], options=timeout_option)
                except DeadlineExceededError:
                    continue
                valid_loss1 += loss1
                valid_acc1 += acc1
                valid_cnt1 += 1
            mean_loss = valid_loss1 / valid_cnt1
            mean_acc = valid_acc1 / valid_cnt1
            print('[epoch:{}] checkpoint loss={:.3f}, acc={:.2f}             '.format(epoch, mean_loss, mean_acc))
            
            # if avg_loss < best_loss * 0.99:
            #     best_loss = avg_loss
            #     saver.save(nn.runtime.get_session(), args.model_path.replace(".npz", ".ckpt"))
            #     print("saving model .. ")
                # if best_counter >= 100:
                #     best_counter = 0
                #     curr_lr /= 2
                #     if curr_lr < 1.e-5:
                #         print('learning rate too small - stopping now')
                #         done = True
                # sess.run(tf.assign(learning_rate, curr_lr))
    
    print("# End of epoch {}".format(epoch))
