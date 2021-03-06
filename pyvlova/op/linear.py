# Copyright 2020 Jiang Shenghu
# SPDX-License-Identifier: Apache-2.0
from tvm import topi

from .base import ArgumentedOp, CombinedOp, OpParameter
from ..poly import TensorTable, ScheduleTree


def schedule(**kwargs):
    init_t = 'stmt_init[n, o]'
    calc_t = 'stmt_calc[n, o, i]'
    output_constraints = '0 <= n < batch and 0 <= o < out_channel'
    calc_constraints = '0 <= i < in_channel'
    domain = '[batch, in_channel, out_channel] -> {' \
             f'{init_t}: {output_constraints}; ' \
             f'{calc_t}: {output_constraints} and {calc_constraints}' \
             '}'
    outer_schedule = '[%s]' % ', '.join(map(
        lambda x: f'{{{init_t}->[({x})];{calc_t}->[({x})]}}', ('n', 'o')))
    inner_schedule = '[%s]' % ', '.join(map(
        lambda x: f'{{{calc_t}->[({x})]}}', ('i',)))

    tree = ScheduleTree.from_yaml(f'''
    domain: "{domain}"
    child:
        schedule: "{outer_schedule}"
        permutable: 1
        coincident: [1, 1]
        child:
            sequence:
              - filter: "{{{init_t}}}"
              - filter: "{{{calc_t}}}"
                child:
                    schedule: "{inner_schedule}"
                    permutable: 1
                    coincident: [1]
    ''')
    tree.apply_params(**dict(filter(lambda x: isinstance(x[1], int), kwargs.items())))
    return tree


class PlainBiasedLinear(ArgumentedOp):
    required_args = [
        'in_channel', 'out_channel',
    ]
    optional_args = {
        'batch': 1,
    }
    tensor_order = ['x', 'weight', 'bias', 'out']
    inputs = ['x', 'weight', 'bias']
    outputs = ['out']
    schedule_factory = schedule

    @staticmethod
    def tensors_factory(batch=1, in_channel=1, out_channel=1, **_):
        table = TensorTable()
        table.add_tensor('x', [batch, in_channel])
        table.add_tensor('weight', [out_channel, in_channel])
        table.add_tensor('bias', [out_channel])
        table.add_tensor('out', [batch, out_channel])
        return table

    @staticmethod
    def statements_factory(**_):
        def stmt_init(t, n, o):
            t['out'][n, o] = t['bias'][o]

        def stmt_calc(t, n, o, i):
            t['out'][n, o] = t['out'][n, o] + t['weight'][o, i] * t['x'][n, i]

        return [stmt_init, stmt_calc]

    def topi_cuda_args(self, x=None, weight=None, bias=None, out=None):
        return [x, weight, bias]

    topi_cuda_task_name = 'dense_small_batch.cuda'
    topi_cuda_calc_func = topi.cuda.dense_small_batch
    topi_cuda_schedule_func = topi.cuda.schedule_dense_small_batch
    topi_cuda_calc_ret_map = ['out']


class PlainLinear(PlainBiasedLinear):
    tensor_order = ['x', 'weight', 'out']
    inputs = ['x', 'weight']

    @staticmethod
    def tensors_factory(batch=1, in_channel=1, out_channel=1, **_):
        table = TensorTable()
        table.add_tensor('x', [batch, in_channel])
        table.add_tensor('weight', [out_channel, in_channel])
        table.add_tensor('out', [batch, out_channel])
        return table

    @staticmethod
    def statements_factory(**_):
        def stmt_init(t, n, o):
            t['out'][n, o] = 0.0

        def stmt_calc(t, n, o, i):
            t['out'][n, o] = t['out'][n, o] + t['weight'][o, i] * t['x'][n, i]

        return [stmt_init, stmt_calc]

    def topi_cuda_args(self, x=None, weight=None, out=None, **kwargs):
        return [x, weight]


class Linear(CombinedOp):
    weight = OpParameter('weight')
    bias = OpParameter('bias')

    def __init__(self, batch=1, in_channel=1, out_channel=1, biased=False, name=''):
        super().__init__(name=name)
        self.biased = biased
        if self.biased:
            factory = PlainBiasedLinear
        else:
            factory = PlainLinear
        self.linear = factory(
            name=self.name + '.linear', batch=batch,
            in_channel=in_channel, out_channel=out_channel
        )
        self._ops.append(self.linear)
        self.weight = self.linear.tensors['weight']
        if self.biased:
            self.bias = self.linear.tensors['bias']
        for i in ['batch', 'out_channel']:
            setattr(self, i, getattr(self.linear, i))

    def calc(self, x):
        if self.biased:
            x = self.linear.calc(x, self.weight, self.bias)
        else:
            x = self.linear.calc(x, self.weight)
        return x
