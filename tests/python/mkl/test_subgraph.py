# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import sys
import os
import mxnet as mx
import numpy as np
import unittest
import ctypes
from mxnet.io import NDArrayIter
from mxnet.module import Module
from mxnet.symbol import Symbol
from importlib import import_module
from numpy.testing import assert_allclose
from mxnet.base import SymbolHandle, check_call, _LIB, mx_uint, c_str
from mxnet.test_utils import DummyIter
curr_path = os.path.dirname(os.path.abspath(os.path.expanduser(__file__)))
sys.path.append(os.path.join(curr_path, '../unittest/'))
from common import with_seed
from mxnet.test_utils import assert_almost_equal
import itertools

OP_NAME='op_name'
QUANTIZED_OP_NAME='quantized_op_name'
SG_PASS_NAME='sg_pass_name'
POST_SG_PASS_NAME='post_sg_pass_name'
config =  {
  'conv': {
    OP_NAME: 'sg_mkldnn_conv',
    QUANTIZED_OP_NAME: 'quantized_sg_mkldnn_conv',
    SG_PASS_NAME: 'MKLDNN',
    POST_SG_PASS_NAME: 'MKLDNN_POST_QUANTIZE'
  },
  'fc': {
    OP_NAME: 'sg_mkldnn_fully_connected',
    QUANTIZED_OP_NAME: 'quantized_sg_mkldnn_fully_connected',
    SG_PASS_NAME: 'MKLDNN',
    POST_SG_PASS_NAME: 'MKLDNN_POST_QUANTIZE'
  }
}

DATA_SHAPE=[(4, 4, 10, 10), (32, 3, 24, 24), (64, 8, 64, 64)]

def check_qsym_calibrated(qsym, out_type, name='conv'):
  quantized_op_name = config[name][QUANTIZED_OP_NAME]
  assert ''.join(qsym.attr_dict().keys()).find(quantized_op_name) != -1
  for k, v in qsym.attr_dict().items():
    if k.find('_quantize') != -1:
      assert v['out_type'] == out_type
    if k.find(quantized_op_name) != -1:
      if name == 'fc' and 'enable_float_output' in v:
        continue
      assert 'min_calib_range' in v
      assert 'max_calib_range' in v

def check_qsym_scale_align(qsym):
  assert ''.join(qsym.attr_dict().keys()).find('quantized_sg_mkldnn_conv') != -1
  init = False
  for k, v in qsym.attr_dict().items():
    if k.find('quantized_sg_mkldnn_conv') != -1:
      assert 'min_calib_range' in v
      assert 'max_calib_range' in v
      if not init:
        min_calib_range = v['min_calib_range']
        max_calib_range = v['max_calib_range']
        init = True
      else:
        assert min_calib_range == v['min_calib_range']
        assert max_calib_range == v['max_calib_range']



def check_qsym_forward(qsym, qarg_params, qaux_params, batch, data_shape, label_shape):
  mod = mx.mod.Module(symbol=qsym, context=mx.current_context())
  mod.bind(for_training=False,
           data_shapes=[('data', data_shape)],
           label_shapes=[('softmax_label', label_shape)])
  mod.set_params(qarg_params, qaux_params)
  mod.forward(batch, is_train=False)
  for output in mod.get_outputs():
    output.wait_to_read()
  return mod.get_outputs()

def check_qsym_dummy_forward(qsym, batch, data_shape, label_shape):
  mod = mx.mod.Module(symbol=qsym, context=mx.current_context())
  mod.bind(for_training=False,
           data_shapes=[('data', data_shape)],
           label_shapes=[('softmax_label', label_shape)])
  mod.init_params(initializer=mx.init.Xavier(magnitude=2.))
  mod.forward(batch, is_train=False)
  for output in mod.get_outputs():
    output.wait_to_read()
  return mod.get_outputs()

def check_qsym_gluon_forward(qsym, qarg_params, qaux_params, data_shape):
  # save qsym to JSON file
  qsym.save('quantized-symbol.json')
  # save params
  save_dict = {('arg:%s' % k): v.as_in_context(mx.current_context()) for k, v in qarg_params.items()}
  save_dict.update({('aux:%s' % k): v.as_in_context(mx.current_context()) for k, v in qaux_params.items()})
  mx.nd.save('quantized-0000.params', save_dict)
  # load back with SymbolBlock
  net = mx.gluon.SymbolBlock.imports('quantized-symbol.json', ['data'], 'quantized-0000.params')
  net.collect_params().reset_ctx(ctx = mx.current_context())
  net.hybridize()

  data = mx.random.uniform(-1.0, 1.0, shape=data_shape)
  net(data)

def check_quantize(sym, data_shape, out_type, name='conv',
                   check_calibration=True, gluon_forward=False, check_scale_align=False):
  sg_pass_name = config[name][SG_PASS_NAME]
  post_sg_pass_name = config[name][POST_SG_PASS_NAME]

  fc = mx.sym.FullyConnected(data=sym, num_hidden=10, flatten=True, name='fc_softmax')
  if gluon_forward == True:
    sym = fc
    sym_sg = sym.get_backend_symbol(sg_pass_name)
    mod = Module(symbol=sym, label_names=[])
    mod.bind(for_training=False,
            data_shapes=[('data', data_shape)])
  else:
    sym = mx.sym.SoftmaxOutput(data=fc, name='softmax')
    sym_sg = sym.get_backend_symbol(sg_pass_name)
    label_shape = (data_shape[0], 10)
    mod = Module(symbol=sym)
    mod.bind(for_training=False,
            data_shapes=[('data', data_shape)],
            label_shapes=[('softmax_label', label_shape)])
  mod.init_params(mx.init.Normal(0.5))
  arg_params, aux_params = mod.get_params()

  data = [mx.random.uniform(-1, 1, shape=shape, ctx=mx.current_context()) for _, shape in mod.data_shapes]
  batch = mx.io.DataBatch(data, [])

  mod.forward(batch, is_train=False)
  for output in mod.get_outputs():
      output.wait_to_read()
  ref_out = mod.get_outputs()

  excluded_sym_names = []
  if mx.current_context() == mx.cpu() and gluon_forward == True:
    excluded_sym_names += ['sg_mkldnn_fully_connected_0']
    excluded_sym_names += ['fc_softmax']

  calib_data = mx.nd.random.uniform(shape=data_shape)
  calib_data = NDArrayIter(data=calib_data)
  calib_data = DummyIter(calib_data)
  calib_layer = lambda name: name.endswith('_output')
  qsym, qarg_params, qaux_params = mx.contrib.quant.quantize_model(sym=sym_sg,
                                                                   arg_params=arg_params,
                                                                   aux_params=aux_params,
                                                                   ctx=mx.current_context(),
                                                                   excluded_sym_names=excluded_sym_names,
                                                                   quantized_dtype=out_type,
                                                                   calib_mode='naive',
                                                                   calib_data=calib_data,
                                                                   calib_layer=calib_layer,
                                                                   num_calib_examples=5)
  qsym = qsym.get_backend_symbol(post_sg_pass_name)
  if check_calibration:
    check_qsym_calibrated(qsym, out_type, name=name)
  if check_scale_align:
    check_qsym_scale_align(qsym)
  if gluon_forward == True:
    check_qsym_gluon_forward(qsym, qarg_params, qaux_params, data_shape)
  else:
    check_qsym_dummy_forward(qsym, batch, data_shape, label_shape)
    quantized_out = check_qsym_forward(qsym, qarg_params, qaux_params, batch, data_shape, label_shape)
    for i in range(len(ref_out)):
      assert_almost_equal(ref_out[i].asnumpy(), quantized_out[i].asnumpy(), atol = 1)

@with_seed()
def check_fusion(sym, data_shape, attrs_op, name='conv', check_quantization=True):
  op_name = config[name][OP_NAME]
  sg_pass_name = config[name][SG_PASS_NAME]

  sym_sg = sym.get_backend_symbol(sg_pass_name)
  assert ''.join(sym_sg.get_internals().list_outputs()).find(op_name) != -1
  for k, v in sym_sg.attr_dict().items():
    if k.find(op_name) != -1:
      for attr_op in attrs_op:
        assert v[attr_op] in ['true', 'True']

  arg_shapes, _, aux_shapes = sym.infer_shape()
  arg_array = [mx.nd.random.uniform(-1, 1, shape=shape) for shape in arg_shapes]
  aux_array = [mx.nd.random.uniform(shape=shape) for shape in aux_shapes]
  exe = sym.bind(ctx=mx.current_context(), args=arg_array, aux_states=aux_array, grad_req='null')
  exe.forward()
  os.environ['MXNET_SUBGRAPH_BACKEND'] = sg_pass_name
  exe_sg = sym.bind(ctx=mx.current_context(), args=arg_array, aux_states=aux_array, grad_req='null')
  exe_sg.forward()
  del os.environ['MXNET_SUBGRAPH_BACKEND']
  for i in range(len(exe.outputs)):
    assert_almost_equal(exe.outputs[i].asnumpy(), exe_sg.outputs[i].asnumpy(), rtol=1e-3, atol=1e-3)

  # fp32 to int8
  out_type_list = ['uint8', 'int8', 'auto']

  if check_quantization:
    for out_type in out_type_list:
      check_quantize(sym, data_shape, out_type, name=name)
      # TODO(ciyong), since quantized fc save its params in int8, while gluon treat the default
      # variable from symbol file as fp32 which results in mismatch dtype of params.
      # Skip quantized fc in gluon pass.
      if name != 'fc':
        check_quantize(sym, data_shape, out_type, name=name, gluon_forward=True)

def check_neg_fusion(syms, attrs_name=None, excluded_attrs=None,
                     date_shape=(4,4,10,10), name='conv'):
  op_name = config[name][OP_NAME]
  sg_pass_name = config[name][SG_PASS_NAME]

  for sym, attrs, excluded_attr in zip(syms, attrs_name, excluded_attrs):
    sym_sg = sym.get_backend_symbol(sg_pass_name)
    exe_sg = sym_sg.simple_bind(mx.cpu(), data=date_shape, grad_req='null')

    attrs_dict = sym_sg.attr_dict()
    for k, v in attrs_dict.items():
      if k.find(op_name) != -1:
        for attr in attrs:
          assert v[attr] == 'true'
        for exc_attr in excluded_attr:
          assert exc_attr not in v.keys()

def head_symbol(data_shape):
  data = mx.symbol.Variable('data', shape=data_shape, dtype='float32')
  weight = mx.symbol.Variable('weight', dtype='float32')
  bn = mx.sym.BatchNorm(data=data, fix_gamma=True, eps=2e-5, momentum=0.9, name='bn')
  return bn, weight

# single conv fuision case
def single_conv(no_bias, data_shape):
  conv_attr = ['']
  data, weight = head_symbol(data_shape)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=no_bias)
  return conv, conv_attr

# conv + bn fusion case
def conv_bn(no_bias, data_shape):
  conv_bn_attr = ['with_bn']
  data, weight = head_symbol(data_shape)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=no_bias)
  bn1 = mx.symbol.BatchNorm(data=conv, name="bn1")
  return bn1, conv_bn_attr

# conv + relu fusion case
def conv_relu(no_bias, data_shape):
  conv_relu_attr = ['with_relu']
  data, weight = head_symbol(data_shape)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=no_bias)
  relu = mx.symbol.Activation(data=conv, name='relu', act_type="relu")
  return relu, conv_relu_attr

# conv + add fusion case
def conv_add(no_bias, data_shape):
  conv_add_attr = ['with_sum']
  data, weight = head_symbol(data_shape)
  conv1 = mx.symbol.Convolution(data=data, weight=weight, name='conv1', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=no_bias)
  conv2 = mx.symbol.Convolution(data=data, name='conv2', num_filter=64,
                               kernel=(3, 3), stride=(1, 1))
  pool = mx.sym.Pooling(data=conv2, kernel=(1, 1), pool_type='avg', name='pool')
  sum = conv1 + pool
  return sum, conv_add_attr

# conv + add fusion case 2
def conv_add2(no_bias, data_shape):
  conv_add_attr = ['with_sum']
  data, weight = head_symbol(data_shape)
  conv1 = mx.symbol.Convolution(data=data, weight=weight, name='conv1', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=no_bias)
  conv2 = mx.symbol.Convolution(data=data, name='conv2', num_filter=64,
                               kernel=(3, 3), stride=(1, 1))
  pool = mx.sym.Pooling(data=conv2, kernel=(1, 1), pool_type='avg', name='pool')
  sum = pool + conv1
  return sum, conv_add_attr

# conv + bn + relu fusion case
def conv_bn_relu(no_bias, data_shape):
  conv_bn_relu_attr = ['with_bn', 'with_relu']
  data, weight = head_symbol(data_shape)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=no_bias)
  bn1 = mx.symbol.BatchNorm(data=conv, name="bn1")
  relu = mx.symbol.Activation(data=bn1, name='relu', act_type="relu")
  return relu, conv_bn_relu_attr

# conv + bn + add + relu fusion case
def conv_bn_sum_relu(no_bias, data_shape):
  conv_bn_add_relu_attr = ['with_sum', 'with_postsum_relu', 'with_bn']
  data, weight = head_symbol(data_shape)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=no_bias)
  bn1 = mx.symbol.BatchNorm(data=conv, name="bn1")
  conv1 = mx.symbol.Convolution(data=data, weight=weight, name='conv1', num_filter=64,
                                kernel=(3, 3), stride=(1, 1))
  sum1 = bn1 + conv1
  relu = mx.symbol.Activation(data=sum1, name='relu', act_type="relu")
  return relu, conv_bn_add_relu_attr

# single concat case
def single_concat(data_shape, input_num, dim):
  data, weight = head_symbol(data_shape)
  inputs = []
  for i in range(input_num):
    inputs.append(data)
  concat = mx.symbol.Concat(*inputs, name="concat", dim=dim)
  return concat

# concat scale alignment case
def concat_scale_align(data_shape):
  data, weight = head_symbol(data_shape)
  conv1 = mx.symbol.Convolution(data=data, weight=weight, name='conv1', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=True)
  conv2 = mx.symbol.Convolution(data=data, weight=weight * 2, name='conv2', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=True)
  conv3 = mx.symbol.Convolution(data=data, weight=weight * 3, name='conv3', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=True)
  conv4 = mx.symbol.Convolution(data=data, weight=weight * 4, name='conv4', num_filter=64,
                               kernel=(3, 3), stride=(1, 1), no_bias=True)
  concat = mx.symbol.Concat(*[conv1, conv2, conv3, conv4], name="concat", dim=1)
  return concat

def tail_neg_symbol(sym1, sym2):
  fc1 = mx.sym.FullyConnected(data=sym1, num_hidden=10, flatten=True, name='fc1')
  fc2 = mx.sym.FullyConnected(data=sym2, num_hidden=10, flatten=True, name='fc2')
  concat = mx.sym.Concat(*[fc1, fc2], name="concat")
  sym = mx.sym.SoftmaxOutput(data=concat, name='softmax')
  return sym

# conv + bn can't be fusion case
# eg.1
# conv --------- > bn
#  |
#  |
#  -------------> [custom op]
def neg_conv_bn(data_shape):
  syms = []
  attrs = []
  excluded_attrs = []
  data, weight = head_symbol(data_shape)

  # eg.1 ([custom op] = pool)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64, kernel=(3, 3), stride=(1, 1))
  bn1 = mx.symbol.BatchNorm(data=conv, name="bn1")
  pool = mx.sym.Pooling(data=conv, kernel=(4, 4), pool_type='avg', name='pool')
  sym = tail_neg_symbol(bn1, pool)

  syms.append(sym)
  attrs.append([])
  excluded_attrs.append([])
  return syms, attrs, excluded_attrs

# conv + relu can't be fusion case
# eg.1
# conv -----------> relu
#  |
#  |
#  ---------------> [custom op]
def neg_conv_relu(data_shape):
  syms = []
  attrs = []
  excluded_attrs = []
  data, weight = head_symbol(data_shape)

  # eg.1 ([custom op] = pool)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64, kernel=(3, 3), stride=(1, 1))
  relu = mx.symbol.Activation(data=conv, name='relu', act_type="relu")
  pool = mx.sym.Pooling(data=conv, kernel=(4, 4), pool_type='avg', name='pool')
  sym = tail_neg_symbol(relu, pool)

  syms.append(sym)
  attrs.append([])
  excluded_attrs.append([])
  return syms, attrs, excluded_attrs

# conv + add can't be fusion case
# eg.1
#  ---------------> [custom op]
#  |
#  |
# conv -----------> add
#                   |
#                   |
# added ------------>
def neg_conv_add(data_shape):
  syms = []
  attrs = []
  excluded_attrs = []
  val = mx.symbol.Variable('addval')
  data, weight = head_symbol(data_shape)

  # eg.1 ([custom op] = pool, [added op] = val)
  conv = mx.symbol.Convolution(data=data, weight=weight, name='conv', num_filter=64, kernel=(3, 3), stride=(1, 1))
  sum1 = conv + val
  pool = mx.sym.Pooling(data=conv, kernel=(4, 4), pool_type='avg', name='pool')
  sym = tail_neg_symbol(sum1, pool)

  syms.append(sym)
  attrs.append([])
  excluded_attrs.append('with_sum')
  return syms, attrs, excluded_attrs

# conv + bn + relu can't be fusion case
# eg.1
#   --------------> [custom op]
#   |
# conv -----------> bn -----------> relu
#
# eg.2
#                   --------------> [custom op]
#                   |
# conv -----------> bn -----------> relu
def neg_conv_bn_relu(data_shape):
  syms = []
  attrs = []
  excluded_attrs = []
  data, weight = head_symbol(data_shape)

  # eg.1 ([custom op] = pool11)
  conv11 = mx.symbol.Convolution(data=data, weight=weight, name='conv11', num_filter=64, kernel=(3, 3), stride=(1, 1))
  bn11 = mx.symbol.BatchNorm(data=conv11, name="bn11")
  relu11 = mx.symbol.Activation(data=bn11, name='relu11', act_type="relu")
  pool11 = mx.sym.Pooling(data=conv11, kernel=(4, 4), pool_type='avg', name='pool11')
  sym1 = tail_neg_symbol(relu11, pool11)

  syms.append(sym1)
  attrs.append([])
  excluded_attrs.append([])

  # eg.2 ([custom op] = pool)
  conv21 = mx.symbol.Convolution(data=data, weight=weight, name='conv21', num_filter=64, kernel=(3, 3), stride=(1, 1))
  bn21 = mx.symbol.BatchNorm(data=conv21, name="bn21")
  relu21 = mx.symbol.Activation(data=bn21, name='relu21', act_type="relu")
  pool21 = mx.sym.Pooling(data=bn21, kernel=(4, 4), pool_type='avg', name='pool21')
  sym2 = tail_neg_symbol(relu21, pool21)

  syms.append(sym2)
  attrs.append(['with_bn'])
  excluded_attrs.append(['with_relu'])
  return syms, attrs, excluded_attrs

# conv + bn + add + relu can't be fusion case
# eg.1
#   --------------> [custom op]
#   |
# conv -----------> bn -----------> add -----------> relu
#
# eg.2
#                    -------------> [custom op]
#                    |
# conv -----------> bn -----------> add -----------> relu
#
# eg.3
#                                    --------------> [custom op]
#                                    |
# conv -----------> bn -----------> add -----------> relu
def neg_conv_bn_add_relu(data_shape):
  syms = []
  attrs = []
  excluded_attrs = []
  addVal = mx.symbol.Variable('addval')
  data, weight = head_symbol(data_shape)

  # eg.1
  conv11 = mx.symbol.Convolution(data=data, weight=weight, name='conv11', num_filter=64, kernel=(3, 3), stride=(1, 1))
  bn11 = mx.symbol.BatchNorm(data=conv11, name="bn11")
  sum11 = bn11 + addVal
  relu11 = mx.symbol.Activation(data=sum11, name='relu11', act_type="relu")
  pool11 = mx.sym.Pooling(data=conv11, kernel=(4, 4), pool_type='avg', name='pool11')
  sym1 = tail_neg_symbol(relu11, pool11)

  syms.append(sym1)
  attrs.append([])
  excluded_attrs.append(['with_sum', 'with_postsum_relu', 'with_bn'])

  # eg.2
  conv21 = mx.symbol.Convolution(data=data, weight=weight, name='conv21', num_filter=64, kernel=(3, 3), stride=(1, 1))
  bn21 = mx.symbol.BatchNorm(data=conv21, name="bn21")
  sum21 = bn21 + addVal
  relu21 = mx.symbol.Activation(data=sum21, name='relu21', act_type="relu")
  pool21 = mx.sym.Pooling(data=bn21, kernel=(4, 4), pool_type='avg', name='pool21')
  sym2 = tail_neg_symbol(relu21, pool21)

  syms.append(sym2)
  attrs.append(['with_bn'])
  excluded_attrs.append(['with_sum', 'with_postsum_relu'])

  # eg.3
  conv31 = mx.symbol.Convolution(data=data, weight=weight, name='conv31', num_filter=64, kernel=(3, 3), stride=(1, 1))
  bn31 = mx.symbol.BatchNorm(data=conv31, name="bn31")
  sum31 = bn31 + addVal
  relu31 = mx.symbol.Activation(data=sum31, name='relu31', act_type="relu")
  pool31 = mx.sym.Pooling(data=sum31, kernel=(4, 4), pool_type='avg', name='pool31')
  sym3 = tail_neg_symbol(relu31, pool31)

  syms.append(sym3)
  attrs.append(['with_bn', 'with_sum'])
  excluded_attrs.append(['with_postsum_relu'])
  return syms, attrs, excluded_attrs

def single_fc(no_bias, data_shape, flatten=True):
  attr = ['']
  data, weight = head_symbol(data_shape)
  fc = mx.symbol.FullyConnected(name='fc', data=data, weight=weight, num_hidden=64,
                                no_bias=no_bias, flatten=flatten)
  return fc, attr

def fc_relu(no_bias, data_shape, flatten=True):
  attr = ['with_relu']
  data, weight = head_symbol(data_shape)
  fc = mx.symbol.FullyConnected(name='fc', data=data, weight=weight, num_hidden=64,
                                no_bias=no_bias, flatten=flatten)
  relu = mx.symbol.Activation(data=fc, name='relu', act_type="relu")
  return relu, attr

# fc + relu can't be fusion case
# eg.1
# fc -----------> relu
#  |
#  |
#  ---------------> [custom op]
def neg_fc_relu(no_bias, data_shape, flatten=True):
  syms = []
  attrs = []
  excluded_attrs = []
  data, weight = head_symbol(data_shape)

  # eg.1 ([custom op] = pool)
  fc = mx.symbol.FullyConnected(name='fc', data=data, weight=weight, num_hidden=64,
                                no_bias=no_bias, flatten=flatten)
  relu = mx.symbol.Activation(data=fc, name='relu', act_type="relu")
  sigmoid = mx.symbol.Activation(data=fc, name='sigmoid', act_type="sigmoid")
  sym = tail_neg_symbol(relu, sigmoid)

  syms.append(sym)
  attrs.append([])
  excluded_attrs.append([])
  return syms, attrs, excluded_attrs

@with_seed()
def test_pos_single_conv():
  for data_shape in DATA_SHAPE:
    net, attrs = single_conv(False, data_shape)
    check_fusion(net, data_shape, attrs)
    net, attrs = single_conv(True, data_shape)
    check_fusion(net, data_shape, attrs)

@with_seed()
def test_pos_conv_relu():
  for data_shape in DATA_SHAPE:
    net, attrs = conv_relu(False, data_shape)
    check_fusion(net, data_shape, attrs)
    net, attrs = conv_relu(True, data_shape)
    check_fusion(net, data_shape, attrs)

@with_seed()
def test_pos_conv_bn():
  for data_shape in DATA_SHAPE:
    net, attrs = conv_bn(False, data_shape)
    check_fusion(net, data_shape, attrs)
    net, attrs = conv_bn(True, data_shape)
    check_fusion(net, data_shape, attrs)

@with_seed()
def test_pos_conv_add():
  for data_shape in DATA_SHAPE:
    net, attrs = conv_add(False, data_shape)
    check_fusion(net, data_shape, attrs)
    net, attrs = conv_add(True, data_shape)
    check_fusion(net, data_shape, attrs)

@with_seed()
def test_pos_conv_add2():
  for data_shape in DATA_SHAPE:
    net, attrs = conv_add2(False, data_shape)
    check_fusion(net, data_shape, attrs)
    net, attrs = conv_add2(True, data_shape)
    check_fusion(net, data_shape, attrs)

@with_seed()
def test_pos_conv_bn_relu():
  for data_shape in DATA_SHAPE:
    net, attrs = conv_bn_relu(False, data_shape)
    check_fusion(net, data_shape, attrs)
    net, attrs = conv_bn_relu(True, data_shape)
    check_fusion(net, data_shape, attrs)

@with_seed()
def test_pos_conv_bn_sum_relu():
  for data_shape in DATA_SHAPE:
    net, attrs = conv_bn_sum_relu(False, data_shape)
    check_fusion(net, data_shape, attrs)
    net, attrs = conv_bn_sum_relu(True, data_shape)
    check_fusion(net, data_shape, attrs)

@with_seed()
def test_pos_single_concat():
  for data_shape in DATA_SHAPE:
    for out_type in ('uint8', 'int8', 'auto'):
      net = single_concat(data_shape, 2, 1)
      check_quantize(net, data_shape, out_type, name='conv', check_calibration=False)
      check_quantize(net, data_shape, out_type, name='conv', check_calibration=False, gluon_forward=True)
      net = single_concat(data_shape, 4, 2)
      check_quantize(net, data_shape, out_type, name='conv', check_calibration=False)
      check_quantize(net, data_shape, out_type, name='conv', check_calibration=False, gluon_forward=True)
      net = single_concat(data_shape, 4, 3)
      check_quantize(net, data_shape, out_type, name='conv', check_calibration=False)
      check_quantize(net, data_shape, out_type, name='conv', check_calibration=False, gluon_forward=True)

@with_seed()
def test_pos_concat_scale_align():
  for data_shape in DATA_SHAPE:
    for out_type in ('uint8', 'int8', 'auto'):
      net = concat_scale_align(data_shape)
      check_quantize(net, data_shape, out_type, check_calibration=True, check_scale_align=True)
      check_quantize(net, data_shape, out_type, check_calibration=True, check_scale_align=True, gluon_forward=True)

@with_seed()
def test_neg_conv_bn():
  for data_shape in DATA_SHAPE:
    syms, attrs, excluded_attrs = neg_conv_bn(data_shape)
    check_neg_fusion(syms, attrs, excluded_attrs, data_shape)

@with_seed()
def test_neg_conv_relu():
  for data_shape in DATA_SHAPE:
    syms, attrs, excluded_attrs = neg_conv_relu(data_shape)
    check_neg_fusion(syms, attrs, excluded_attrs, data_shape)

@with_seed()
def test_neg_conv_add():
  for data_shape in DATA_SHAPE:
    syms, attrs, excluded_attrs = neg_conv_add(data_shape)
    check_neg_fusion(syms, attrs, excluded_attrs, data_shape)

@with_seed()
def test_neg_conv_bn_relu():
  for data_shape in DATA_SHAPE:
    syms, attrs, excluded_attrs = neg_conv_bn_relu(data_shape)
    check_neg_fusion(syms, attrs, excluded_attrs, data_shape)

@with_seed()
def test_neg_conv_bn_add_relu():
  for data_shape in DATA_SHAPE:
    syms, attrs, excluded_attrs = neg_conv_bn_add_relu(data_shape)
    check_neg_fusion(syms, attrs, excluded_attrs, data_shape)

@with_seed()
def test_single_fc():
  for dshape, no_bias, flatten in itertools.product(DATA_SHAPE, [True, False], [True, False]):
    syms, attrs = single_fc(no_bias, dshape, flatten)
    if flatten is True:
      check_fusion(syms, dshape, attrs, name='fc', check_quantization=True)
    else:
      check_fusion(syms, dshape, attrs, name='fc', check_quantization=False)


@with_seed()
def test_fc_relu():
  for dshape, no_bias, flatten in itertools.product(DATA_SHAPE, [True, False], [True, False]):
    syms, attrs = fc_relu(no_bias, dshape, flatten)
    if flatten is True:
      check_fusion(syms, dshape, attrs, name='fc', check_quantization=True)
    else:
      check_fusion(syms, dshape, attrs, name='fc', check_quantization=False)

@with_seed()
def test_neg_fc_relu():
  for dshape, no_bias, flatten in itertools.product(DATA_SHAPE, [True, False], [True, False]):
    syms, attrs, excluded_attrs = neg_fc_relu(no_bias, dshape, flatten)
    check_neg_fusion(syms, attrs, excluded_attrs, dshape, name='fc')

if __name__ == "__main__":
  import nose
  nose.runmodule()
