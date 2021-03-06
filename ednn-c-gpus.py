# -*- coding: utf-8 -*-

from __future__ import print_function

import tensorflow as tf
from ednn_helper import EDNN_helper
import h5py
import os
import numpy as np
import argparse
from tensorflow.python.training.moving_averages import assign_moving_average


def batch_norm(x, train, eps=1e-05, decay=0.9, affine=True, name=None):
    with tf.variable_scope(name, default_name='BatchNorm2d'):
        params_shape = tf.shape(x)[-1:]
        moving_mean = tf.get_variable('mean', params_shape,
                                      initializer=tf.zeros_initializer,
                                      trainable=False)
        moving_variance = tf.get_variable('variance', params_shape,
                                          initializer=tf.ones_initializer,
                                          trainable=False)

        def mean_var_with_update():
            mean, variance = tf.nn.moments(x, tf.shape(x)[:-1], name='moments')
            with tf.control_dependencies([assign_moving_average(moving_mean, mean, decay),
                                          assign_moving_average(moving_variance, variance, decay)]):
                return tf.identity(mean), tf.identity(variance)
        mean, variance = tf.cond(train, mean_var_with_update, lambda: (moving_mean, moving_variance))
        if affine:
            beta = tf.get_variable('beta', params_shape,
                                   initializer=tf.zeros_initializer)
            gamma = tf.get_variable('gamma', params_shape,
                                    initializer=tf.ones_initializer)
            x = tf.nn.batch_normalization(x, mean, variance, beta, gamma, eps)
        else:
            x = tf.nn.batch_normalization(x, mean, variance, None, None, eps)
        return x


def NN_coarse(_in):
    tile_size = f_c + 2*c_c
    # default is 576
    _in = tf.reshape(_in, (-1, tile_size**2))
    nn = tf.contrib.layers.fully_connected(_in, 64, reuse=False, scope='ful1')
    nn = tf.contrib.layers.fully_connected(_in, 128, reuse=False, scope='ful2')
    nn = tf.contrib.layers.fully_connected(nn, 128, reuse=False, scope='ful3')
    nn = tf.nn.dropout(nn, keep_prob=dropout, name='dp1')
    nn = tf.contrib.layers.fully_connected(nn, 1, activation_fn=None, reuse=False,
                                           scope='ful4')
    return nn


def average_tower_grads( tower_grads):
    if(len(tower_grads) == 1):
      return tower_grads[0]
    avgGrad_var_s = []
    for grad_var_s in zip(*tower_grads):
      grads = []
      v = None
      for g, v_ in grad_var_s:
        g = tf.expand_dims(g, 0)
        grads.append(g)
        v = v_
      all_g = tf.concat(grads, 0)
      avg_g = tf.reduce_mean(all_g, 0, keep_dims=False)
      avgGrad_var_s.append((avg_g, v))
    return avgGrad_var_s


def build_model(L, f, c, train_data, train_labels,
                valid_data, valid_labels,  save_dir, 
                BATCH_SIZE=5000, EPOCHS=5000, report=10, num_gpus=2,
                learning_rate=0.001):
    a = tf.Graph()
    with a.as_default():
      with tf.device("/cpu:0"):
          # data comes in a [ batch * L * L ] tensor, and labels a [ batch * 1] tensor
          x = tf.placeholder(tf.float32, (num_gpus, None, L, L), name='input_image')
          y = tf.placeholder(tf.float32, (num_gpus, None, 1))
          helper = EDNN_helper(L=L, f=f, c=c)
          optimizer = tf.train.AdamOptimizer(learning_rate)
          towerGrads = []
          towerloss = []

      with tf.variable_scope('train') as scope:
          for i in range(num_gpus):
              with tf.device("/gpu:%d" % i):
                  with tf.name_scope('tower_%d' % i) as scope:
                      # Then the EDNN-specific code:
                      tiles = tf.map_fn(helper.ednn_split, x[i], back_prop=False)
                      tiles = tf.transpose(tiles, perm=[1, 0, 2, 3, 4])
                      output = tf.map_fn(NN_coarse, tiles, back_prop=True)
                      output = tf.transpose(output, perm=[1, 0, 2])
                      predicted = tf.reduce_sum(output, axis=1)
                      # define the loss function
                      vars = tf.trainable_variables()
                      lossL2 = tf.add_n([tf.nn.l2_loss(v) for v in vars
                                         #if 'bias' not in v.name]) * beta
                      loss_ = tf.reduce_mean(tf.square(y[i] - predicted)) + lossL2
                      towerloss.append(loss_)
                      towerGrads.append(optimizer.compute_gradients(loss_))
                      tf.get_variable_scope().reuse_variables()
      # average the losses and gradients
      avg_Grads = average_tower_grads(towerGrads)
      train_step = optimizer.apply_gradients(avg_Grads)
      loss = tf.reduce_sum(towerloss) / num_gpus

      # define session
      config = tf.ConfigProto()
      config.gpu_options.allow_growth = True
      sess = tf.InteractiveSession(config=config)

      # add loss in summary
      #tf.summary.scalar('loss', loss)
      #merged = tf.summary.merge_all()
      #train_writer = tf.summary.FileWriter(summary_dir + '/train', sess.graph)
      #valid_writer = tf.summary.FileWriter(summary_dir + '/valid')

      sess.run(tf.global_variables_initializer())
      saver = tf.train.Saver()
      a.finalize()

    # constants
    model_path = os.path.join(save_dir, 'model')
    epochs = []
    losses_tra = []
    losses_val = []
    num_bunches = int((train_data.shape[0] / BATCH_SIZE) // num_gpus)
    bunch_size_v = valid_data.shape[0] // num_gpus
    # start training
    for epoch in range(EPOCHS):
        for bunch in range(num_bunches):
            # assign train data feeds
            train_feeds = {}
            train_feeds[x] = []
            train_feeds[y] = []
            BUNCH_SIZE = BATCH_SIZE*num_gpus
            for gpu in range(num_gpus):
                train_feeds[x].append(train_data[bunch*BUNCH_SIZE+
                    gpu*BATCH_SIZE:bunch*BUNCH_SIZE+(gpu+1)*BATCH_SIZE])
                train_feeds[y].append(train_labels[bunch*BUNCH_SIZE+
                    gpu*BATCH_SIZE:bunch*BUNCH_SIZE+(gpu+1)*BATCH_SIZE])

            _, loss_tra = sess.run([train_step, loss], train_feeds)

        if epoch % report == 0:
            # assign valid data feeds
            valid_feeds = {}
            valid_feeds[x] = []
            valid_feeds[y] = []
            for gpu in range(num_gpus):
                valid_feeds[x].append(valid_data[gpu*bunch_size_v:
                                                  (gpu+1)*bunch_size_v])
                valid_feeds[y].append(valid_labels[gpu*bunch_size_v:
                                                  (gpu+1)*bunch_size_v])

            loss_val, = sess.run([loss], valid_feeds)

            #train_writer.add_summary(summary1, epoch)
            #valid_writer.add_summary(summary2, epoch)

            print("epoch: " + str(epoch) + ' | training loss: ' + str(loss_tra) \
                      + ' | validation loss: ' + str(loss_val) + " (model saved)")

            saver.save(sess, model_path)
            epochs.append(epoch)
            losses_tra.append(loss_tra)
            losses_val.append(loss_val)
    return epochs, losses_tra, losses_val


def load_data(path):
    open_path = path + '/avg.hdf5'
    f1 = h5py.File(open_path, 'r')
    open_path = path + '/energy.hdf5'
    f2 = h5py.File(open_path, 'r')
    return f1['avg_data'], np.reshape(f2['elec'], [-1, 1])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('save', help='save directory')
    parser.add_argument('data', help='where you saved your data')
    parser.add_argument('-L', help='data size(default: 32)', type=int,
                        default=32, dest='L')
    parser.add_argument('-f', help='focus size of coarse model(default: 8)',
                        type=int, default=8, dest='f_c')
    parser.add_argument('-c', help='context size of coarse model(default: 8)',
                        type=int, default=8, dest='c_c')
    parser.add_argument('-e', help='number of epochs(default: 5000)',
                        type=int, default=5000, dest='EPOCHS')
    parser.add_argument('-b', help='batch size(default: 5000)',
                        type=int, default=5000, dest='BATCH')
    parser.add_argument('-r', help='report interval(default: 10)',
                        type=int, default=10, dest='report')
    parser.add_argument('-g', help='number of gpus(default: 2)',
                        type=int, default=2, dest='num_gpus')
    parser.add_argument('-l', help='learning rate(default: 0.001)',
                        type=float, default=0.001, dest='lr')
    parser.add_argument('-d', help='dropout keep probability(default: 1)', dest='dropout',
                        type=float, default=1)
    args = parser.parse_args()

    f_c = args.f_c
    c_c = args.c_c
    dropout = args.dropout

    path = args.data + '/train-data'
    train_data, train_labels = load_data(path)

    path = args.data + '/valid-data'
    valid_data, valid_labels = load_data(path)

    # train model
    epochs, losses_tra, losses_val = build_model(args.L, args.f_c, args.c_c,
                        train_data, train_labels, valid_data, valid_labels,
                        args.save, args.BATCH, args.EPOCHS,
                        args.report, args.num_gpus, args.lr)

    # save losses
    loss_f = h5py.File('coarse_losses.hdf5', 'w')
    loss_f['epochs'] = epochs
    loss_f['train'] = losses_tra
    loss_f['valid'] = losses_val
