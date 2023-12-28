import math
from utils.distributed import master_only_print as mprint

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function
from torch.nn.modules.utils import _pair
from utils.gumbel import gumbel_softmax
from time import sleep

from utils.config import FLAGS


def bn_calibration(m):
    if getattr(m, 'track_running_stats', False):
        m.reset_running_stats()
        m.train()
        #m.training = True
        if getattr(FLAGS, 'cumulative_bn_stats', False):
            m.momentum = None
    # if isinstance(m, SwitchableBatchNorm2d):
    #     m.idx = 0


def out_shape(i, p, d, k, s):
    return (i + 2 * p - d * (k - 1) - 1) // s + 1


class Round(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        return torch.round(input)

    @staticmethod
    def backward(ctx, grad_output):
        grad_input = grad_output.clone()
        return grad_input


class EMA():
    def __init__(self, decay):
        self.decay = decay
        self.shadow = {}
        self.param_copy = {}
        self.ignore_model_profiling = True

    def shadow_register(self, model, bn_stats=True):
        for name, param in model.named_parameters():
            if param.requires_grad:
                self.shadow[name] = param.data.clone()
        if bn_stats:
            bn_idx = 0
            for m in model.modules():
                if isinstance(m, nn.BatchNorm2d):
                    self.shadow['bn{}_mean'.format(bn_idx)] = m.running_mean.clone()
                    self.shadow['bn{}_var'.format(bn_idx)] = m.running_var.clone()
                    bn_idx += 1

    def shadow_update(self, model, bn_stats=True):
        for name, param in model.named_parameters():
            if param.requires_grad:
                #assert name in self.shadow, '{} is not in {}.'.format(name, self.shadow.keys())
                self.shadow[name] -= (1.0 - self.decay) * (self.shadow[name] - param.data)
        if bn_stats:
            bn_idx = 0
            for m in model.modules():
                if isinstance(m, nn.BatchNorm2d):
                    #assert 'bn{}_mean'.format(bn_idx) in self.shadow, '{} is not in {}.'.format('bn{}_mean'.format(bn_idx), self.shadow.keys())
                    self.shadow['bn{}_mean'.format(bn_idx)] -= (1.0 - self.decay) * (self.shadow['bn{}_mean'.format(bn_idx)] - m.running_mean)
                    #assert 'bn{}_var'.format(bn_idx) in self.shadow, '{} is not in {}.'.format('bn{}_var'.format(bn_idx), self.shadow.keys())
                    self.shadow['bn{}_var'.format(bn_idx)] -= (1.0 - self.decay) * (self.shadow['bn{}_var'.format(bn_idx)] - m.running_var)
                    bn_idx += 1

    def shadow_apply(self, model, bn_stats=True):
        for name, param in model.named_parameters():
            if param.requires_grad:
                self.param_copy[name] = param.data.clone()
                param.data.copy_(self.shadow[name])
        if bn_stats:
            bn_idx = 0
            for m in model.modules():
                if isinstance(m, nn.BatchNorm2d):
                    self.param_copy['bn{}_mean'.format(bn_idx)] = m.running_mean.clone()
                    m.running_mean.copy_(self.shadow['bn{}_mean'.format(bn_idx)])
                    self.param_copy['bn{}_var'.format(bn_idx)] = m.running_var.clone()
                    m.running_var.copy_(self.shadow['bn{}_var'.format(bn_idx)])
                    bn_idx += 1

    def weight_recover(self, model, bn_stats=True):
        for name, param in model.named_parameters():
            if param.requires_grad:
                param.data.copy_(self.param_copy[name])
        if bn_stats:
            bn_idx = 0
            for m in model.modules():
                if isinstance(m, nn.BatchNorm2d):
                    m.running_mean.copy_(self.param_copy['bn{}_mean'.format(bn_idx)])
                    m.running_var.copy_(self.param_copy['bn{}_var'.format(bn_idx)])
                    bn_idx += 1
        self.param_copy = {}


class Quantize_k(Function):
    """
        This is the quantization function.
        The input and output should be all on the interval [0, 1].
        bit is only defined on nonnegative integer values.
        zero_point is the value used for 0-bit, and should be on the interval [0, 1].
    """
    @staticmethod
    def forward(ctx, input, bit=torch.tensor([8]), align_dim=0, zero_point=0, val=0, scheme='original'):
        #assert torch.all(bit >= 0)
        #try: 
        #    assert torch.all(input >= 0) 
        #except AssertionError as e:
        #    print(input[input>1].tolist())
        #    exit()
            #and torch.all(input <= 1)
        #assert zero_point >= 0 and zero_point <= 1
        #print(bit)
        if scheme in ['original', 'simple_interpolation', 'bitwidth_aggregation', 'bitwidth_direct']:
            a = torch.pow(2, bit) - 1
            expand_dim = input.dim() - align_dim - 1
            a = a[(...,) + (None,) * expand_dim]
            res = torch.round(a * input)
            res.div_(1 + torch.relu(a - 1))
            res.add_(zero_point * torch.relu(1 - a))
            #print(res)
            #if scheme == 'original':
            #    assert torch.all(res <= 1)

        elif scheme == 'stepsize_aggregation':
            s = val
            expand_dim = input.dim() - align_dim - 1
            s = s[(...,) + (None,) * expand_dim]
            res = torch.round(input / s)
            res.mul_(s)

        elif scheme == 'nlvs_aggregation':
            a = val
            expand_dim = input.dim() - align_dim - 1
            a = a[(...,) + (None,) * expand_dim]
            res = torch.round(a * input)
            res.div_(1 + torch.relu(a - 1))
            res.add_(zero_point * torch.relu(1 - a))

        elif scheme == 'nlvs_direct':
            a = val
            expand_dim = input.dim() - align_dim - 1
            a = a[(...,) + (None,) * expand_dim]
            res = torch.round(a * input)
            res.div_(1 + torch.relu(a - 1))
            res.add_(zero_point * torch.relu(1 - a))
            #assert torch.all(res <= 1)

        elif scheme == 'modified':
            a = torch.pow(2, bit)
            expand_dim = input.dim() - align_dim - 1
            a = a[(...,) + (None,) * expand_dim]
            res = torch.floor(a * input)
            res = torch.clamp(res, max=a - 1)
            res.div_(a)
            res.add_(zero_point * torch.relu(2 - a))
            assert torch.all(res <= 1)
        else:
            print(scheme)
            raise NotImplementedError
        #try:
        #    assert torch.all(res >= 0)
        #except:
        #    print(res)
        #    print(s)
        #    print(e)
        #    exit()
        #try:
        #    assert torch.all(res <= 1)
        #cexcept AssertionError as e:
        #    print(res[res>1].tolist())
        #    print(e)
        #    exit()
        return res

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output, None, None, None, None, None


class QuantizableConv2d(nn.Conv2d):
    def __init__(self, in_channels, out_channels,
                 kernel_size, stride=1, padding=0, dilation=1,
                 groups=1, bias=True,
                 same_padding=False,
                 lamda_w_min=None, lamda_a_min=None,
                 double_side=False,
                 weight_only=False,
                 input_size=(0, 0)):
        super(QuantizableConv2d, self).__init__(
            in_channels, out_channels,
            kernel_size, stride=stride,
            padding=padding if not same_padding else 0,
            dilation=dilation,
            groups=groups, bias=bias)
        self.same_padding = same_padding
        self.lamda_w_min = lamda_w_min
        self.lamda_a_min = lamda_a_min
        self.double_side = double_side
        self.weight_only = weight_only or getattr(FLAGS, 'weight_only', False)
        self.quant = Quantize_k.apply
        self.alpha = nn.Parameter(torch.tensor(8.0))
        init_bit = getattr(FLAGS, 'init_bit', 7.5)
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_weight', False):
            self.lamda_w = nn.Parameter(torch.ones(self.out_channels) * init_bit)
        else:
            self.lamda_w = nn.Parameter(torch.tensor(init_bit))
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_activation', False):
            self.lamda_a = nn.Parameter(torch.ones(self.in_channels) * init_bit)
        else:
            self.lamda_a = nn.Parameter(torch.tensor(init_bit))
        self.eps = 0.00001
        self.input_size = input_size
        qtype_list = ['stepsize_aggregation', 'bitwidth_aggregation', 'bitwidth_direct', 
                      'simple_interpolation', 'stepsize_aggregation', 'nlvs_aggregation', 
                      'nlvs_direct']
        self.quant_type = None
        for name in qtype_list:
            if getattr(FLAGS, name, False):
                self.quant_type = name
        if self.quant_type == None:
            self.quant_type = 'original'
        if getattr(FLAGS, 'nlvs_direct', False):
            if lamda_w_min == None:
                self.nlvs_w = nn.Parameter(torch.tensor(2 ** init_bit))
            if lamda_a_min == None:
                self.nlvs_a = nn.Parameter(torch.tensor(2 ** init_bit))
        if getattr(FLAGS, 'L_value', 0) == 'learned':
            self.gamma = nn.Parameter(torch.tensor(getattr(FLAGS, 'L_init', 1.0)))
        self.sigma = getattr(FLAGS, 'window_size', 2) / 2
        if getattr(FLAGS, 'grad_ema_alpha', False):
            self.ema_lamda_w_grad = nn.Parameter(torch.tensor(0.01), requires_grad=False)
            self.ema_lamda_a_grad = nn.Parameter(torch.tensor(0.01), requires_grad=False)
        

    def forward(self, input):
        if getattr(FLAGS, 'full_precision', False):
            return nn.functional.conv2d(
            input, self.weight, self.bias, self.stride, self.padding,
            self.dilation, self.groups)

        if self.same_padding:
            ih, iw = input.size()[-2:]
            kh, kw = self.weight.size()[-2:]
            sh, sw = self.stride
            oh, ow = math.ceil(ih / sh), math.ceil(iw / sw)
            pad_h = max((oh - 1) * self.stride[0] + (kh - 1) * self.dilation[0] + 1 - ih, 0)
            pad_w = max((ow - 1) * self.stride[1] + (kw - 1) * self.dilation[1] + 1 - iw, 0)
            if pad_h > 0 or pad_w > 0:
                input = nn.functional.pad(input, [pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2])

        if self.lamda_w < min(FLAGS.bits_list):
            self.lamda_w.data = nn.Parameter(torch.tensor(float(min(FLAGS.bits_list)))).to(self.weight.device)
        elif self.lamda_w > max(FLAGS.bits_list):
            self.lamda_w.data = nn.Parameter(torch.tensor(float(max(FLAGS.bits_list)))).to(self.weight.device)

        if self.lamda_a < min(FLAGS.bits_list):
            self.lamda_a.data = nn.Parameter(torch.tensor(float(min(FLAGS.bits_list)))).to(input.device)
        elif self.lamda_a > max(FLAGS.bits_list):
            self.lamda_a.data = nn.Parameter(torch.tensor(float(max(FLAGS.bits_list)))).to(input.device)

        lamda_w = self.lamda_w
        lamda_a = self.lamda_a

        #if getattr(FLAGS, 'hard_assignment',False):
        #    lamda_w = torch.round(lamda_w + FLAGS.hard_offset).detach()
        #    lamda_a = torch.round(lamda_a + FLAGS.hard_offset).detach()
        #lamda_w = torch.clamp(lamda_w, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if self.lamda_w_min is not None:
            lamda_w = torch.clamp(lamda_w, min=self.lamda_w_min)
        act_bits_list = getattr(FLAGS,'act_bits_list',FLAGS.bits_list)
        #lamda_a = torch.clamp(self.lamda_a, min(act_bits_list), max(act_bits_list))
        if self.lamda_a_min is not None:
            lamda_a = torch.clamp(lamda_a, min=self.lamda_a_min)
        
        weight_quant_scheme = self.quant_type
        act_quant_scheme = self.quant_type

        ### Weight quantization
        weight = torch.tanh(self.weight) / torch.max(torch.abs(torch.tanh(self.weight)))
        weight.add_(1.0)
        weight.div_(2.0)
        if getattr(FLAGS, 'hard_assignment', False):
            weight = self.quant(weight, lamda_w.detach(), 0, 0.5, 0, 'original')
        elif getattr(FLAGS, 'simple_interpolation', False):
            # ------- introduce: general distance-based interpolation 
            #######    window size setting    ######
            if getattr(FLAGS, 'nlvs_direct', False):
                weight = self.quant(weight, torch.zeros(1), 0, 0.5, self.nlvs_w, weight_quant_scheme)
                self.lamda_w.data = torch.log2(self.nlvs_w)
            elif getattr(FLAGS, 'bitwidth_direct', False):
                if getattr(FLAGS, 'hard_forward', False):
                    lamda_w = Round.apply(lamda_w)
                weight = self.quant(weight, lamda_w, 0, 0.5, 0, weight_quant_scheme)
            else:
                window_size = getattr(FLAGS, 'window_size', 2)
                weight_bits_tensor_list = torch.Tensor(FLAGS.bits_list).to(weight.device)
                if getattr(FLAGS, 'distance_v1', False):
                    m = 1. / (torch.abs(lamda_w - weight_bits_tensor_list) + self.eps)
                elif getattr(FLAGS, 'distance_v2', False):
                    m = torch.relu(torch.Tensor([window_size/2]).to(weight.device).view(-1, 1) \
                            - (torch.abs(lamda_w.view(-1, 1) - weight_bits_tensor_list.view(-1, 1))))
                elif getattr(FLAGS, 'distance_gaussian', False):
                    # 1. remake bits tensor using ceil and floor
                    bits_list_new = torch.Tensor(
                                    [torch.floor(lamda_w-2),
                                     torch.floor(lamda_w-1), 
                                     torch.floor(lamda_w), 
                                     torch.ceil(lamda_w),
                                     torch.ceil(lamda_w+1),
                                     torch.ceil(lamda_w+2)]).to(weight.device)
                    # 2. filter bits_list_new by magnitude
                    bits_list_new = bits_list_new[bits_list_new > min(FLAGS.bits_list)]
                    weight_bits_tensor_list = bits_list_new[bits_list_new < max(FLAGS.bits_list)]
                    m = torch.exp(-torch.pow(weight_bits_tensor_list-lamda_w, 2)/2*(1/self.sigma)*(1/self.sigma))
                else:
                    m = torch.zeros_like(weight_bits_tensor_list)
                L = getattr(FLAGS, 'L_value', 1)
                if L == 'learned':
                    m = torch.pow(m, self.gamma)
                elif L == 'softmax':
                    tau = getattr(FLAGS, 'tau', 0)
                    m = F.softmax(m / tau, dim=0)
                else:
                    m = torch.pow(m, L)
                
                f = m > 0.05
                m = m[f]
                weight_bits_tensor_list = weight_bits_tensor_list.view(-1, 1)[f]
                p = m / m.sum(dim=0, keepdim=True)

                    ##################################################
                if self.lamda_w_min == 8:
                    weight = self.quant(weight, lamda_w, 0, 0.5, 0, 'simple_interpolation')
                elif getattr(FLAGS, 'stepsize_aggregation', False):
                    stepsize_tensor_list = 1 / (torch.pow(2, weight_bits_tensor_list)-1)
                    s = torch.dot(p.view(-1), stepsize_tensor_list)
                    weight = self.quant(weight, torch.zeros(1), 0, 0.5, s, weight_quant_scheme)
                elif getattr(FLAGS, 'nlvs_aggregation', False):
                    a = torch.dot(p.view(-1), torch.pow(2, weight_bits_tensor_list)-1)
                    input_val = self.quant(weight, torch.zeros(1), 0, 0.5, a, weight_quant_scheme)
                elif getattr(FLAGS, 'bitwidth_aggregation', False):
                    interpolated_bit = sum([p[i] * weight_bits_tensor_list[i] for i in range(len(p))])
                    weight = self.quant(weight, interpolated_bit, 0, 0.5, 0, weight_quant_scheme)
                else:
                    weight_temp = 0
                    for i, bit in enumerate(weight_bits_tensor_list):
                        weight_temp += p[i].view(-1, 1, 1, 1) * self.quant(weight, bit, 0, 0.5, 0, weight_quant_scheme)
                    weight = weight_temp
            # -------------------------------------------------------
        else:
            p_l = 1 + torch.floor(lamda_w) - lamda_w
            p_h = 1 - p_l

            if not getattr(FLAGS, 'hard_assignment', False) and getattr(FLAGS,'gumbel_softmax',False):
                logits = torch.Tensor([torch.log(p_l)+ self.eps, torch.log(p_h) + self.eps]).view(1,2).cuda()
                one_hot = gumbel_softmax(logits, getattr(FLAGS, 'temperature',1.0))
                p_l = one_hot[0,0]
                p_h = one_hot[0,1]
            weight = p_h.view(-1,1,1,1) * self.quant(weight, torch.ceil(lamda_w), 0, 0.5, 0, weight_quant_scheme) \
                + p_l.view(-1,1,1,1) * self.quant(weight, torch.floor(lamda_w), 0, 0.5, 0, weight_quant_scheme)
        weight.mul_(2.0)
        weight.sub_(1.0)
        if getattr(FLAGS, 'rescale_conv', False):
            rescale_type = getattr(FLAGS, 'rescale_type', 'constant')
            if rescale_type == 'stddev':
                weight_scale = torch.std(self.weight.detach())
            elif rescale_type == 'constant':
                weight_scale = 1.0 / (self.out_channels * self.kernel_size[0] * self.kernel_size[1]) ** 0.5
            else:
                raise NotImplementedError
            weight_scale /= torch.std(weight.detach())
            weight.mul_(weight_scale)

        ### Activation quantization
        if self.weight_only:
            input_val = input
        else:
            if self.double_side:
                input_val = torch.where(input > -torch.abs(self.alpha), input, -torch.abs(self.alpha).type(input.type()))
            else:
                input_val = torch.relu(input)
            input_val = torch.where(input_val < torch.abs(self.alpha), input_val, torch.abs(self.alpha).type(input.type()))
            input_val.div_(torch.abs(self.alpha))
            if self.double_side:
                input_val.add_(1.0)
                input_val.div_(2.0)

            # ------- introduce: general distance-based interpolation
            if getattr(FLAGS, 'hard_assignment', False):
                input_val = self.quant(input_val, lamda_a.detach(), 1, 0, 0, 'original')
            elif getattr(FLAGS, 'simple_interpolation', False):
                if getattr(FLAGS, 'bitwidth_direct', False):
                    if getattr(FLAGS, 'hard_forward', False):
                        lamda_a = Round.apply(lamda_a)
                    input_val = self.quant(input_val, lamda_a, 1, 0, 0, act_quant_scheme)
                elif getattr(FLAGS, 'nlvs_direct', False):
                    input_val = self.quant(input_val, torch.zeros(1), 1, 0, self.nlvs_a, weight_quant_scheme)
                    self.lamda_a.data = torch.log2(self.nlvs_a)
                else:
                    window_size = getattr(FLAGS, 'window_size', 2)
                    act_bits_tensor_list = torch.Tensor(act_bits_list).to(input_val.device)
                    if getattr(FLAGS, 'distance_v1', False):
                        m = 1. / (torch.abs(lamda_a - act_bits_tensor_list + self.eps))
                    elif getattr(FLAGS, 'distance_v2', False):
                        m = torch.relu(torch.Tensor([window_size/2]).to(input_val.device) - (torch.abs(lamda_a - act_bits_tensor_list)))
                        #m = torch.relu(torch.Tensor([window_size/2]).to(input_val.device).view(-1, 1) - (torch.abs(lamda_a.view(-1,1) - act_bits_tensor_list.view(-1,1))))
                    elif getattr(FLAGS, 'distance_gaussian', False):
                        bits_list_new = torch.Tensor(
                                        [torch.floor(lamda_a-2),
                                        torch.floor(lamda_a-1), 
                                        torch.floor(lamda_a), 
                                        torch.ceil(lamda_a),
                                        torch.ceil(lamda_a+1),
                                        torch.ceil(lamda_a+2)]).to(input_val.device)
                        bits_list_new = bits_list_new[bits_list_new > min(FLAGS.bits_list)]
                        act_bits_tensor_list = bits_list_new[bits_list_new < max(FLAGS.bits_list)]
                        m = torch.exp(-torch.pow(act_bits_tensor_list-lamda_a, 2)/2*(1/self.sigma)*(1/self.sigma))
                    else:
                        m = torch.zeros_like(act_bits_tensor_list)
                    L = getattr(FLAGS, 'L_value', 1)
                    if L == 'learned':
                        m = torch.pow(m, self.gamma)
                    elif L == 'softmax':
                        tau = getattr(FLAGS, 'tau', 0)
                        m = F.softmax(m / tau, dim=0)
                    else:
                        m = torch.pow(m, L)
                    f = m > 0.05
                    m = m[f]
                    act_bits_tensor_list = act_bits_tensor_list[f]
                    p = m / m.sum(dim=0, keepdim=True)

                    ##################################################
                    if self.lamda_a_min == 8:
                        input_val = self.quant(input_val, lamda_a, 1, 0, 0, 'simple_interpolation')
                    elif getattr(FLAGS, 'stepsize_aggregation', False):
                        stepsize_tensor_list = 1/(torch.pow(2, act_bits_tensor_list)-1)
                        s = torch.dot(p.view(-1), stepsize_tensor_list)
                        input_val = self.quant(input_val, torch.zeros(1), 1, 0, s, act_quant_scheme)
                    elif getattr(FLAGS, 'nlvs_aggregation', False):
                        a = torch.dot(p.view(-1), (torch.pow(2, act_bits_tensor_list)-1))
                        input_val = self.quant(input_val, torch.zeros(1), 1, 0, a, act_quant_scheme)
                    elif getattr(FLAGS, 'bitwidth_aggregation', False):
                        interpolated_bit = sum([p[i] * act_bits_tensor_list[i] for i in range(len(p))])
                        input_val = self.quant(input_val, interpolated_bit, 1, 0, 0, act_quant_scheme)
                    else:
                        input_temp = 0
                        for i , bit in enumerate(act_bits_tensor_list):
                            quant_temp = self.quant(input_val, bit, 1, 0, 0, act_quant_scheme)
                            input_temp += p[i].view(-1, 1, 1) * quant_temp
                        input_val = input_temp
                # -------------------------------------------------------
            else:
                p_a_l = 1 + torch.floor(lamda_a) - lamda_a
                p_a_h = 1 - p_a_l
                #print(f'{lamda_a.item():.4f}, {p_a_l.item():.4f}:{p_a_h.item():.4f}')
                # The sentences below are not executed in fracbits paper ver.
                if not getattr(FLAGS, 'hard_assignment', False) and getattr(FLAGS,'gumbel_softmax',False):
                    logits = torch.Tensor([torch.log(p_a_l)+ self.eps, torch.log(p_a_h) + self.eps]).view(1,2).cuda()
                    one_hot = gumbel_softmax(logits, getattr(FLAGS, 'temperature',1.0))
                    p_a_l = one_hot[0,0]
                    p_a_h = one_hot[0,1]
                input_val = p_a_h.view(-1,1,1) * self.quant(input_val, torch.ceil(lamda_a), 1, 0, 0, act_quant_scheme) \
                    + p_a_l.view(-1,1,1) * self.quant(input_val, torch.floor(lamda_a), 1, 0, 0, act_quant_scheme)
            if self.double_side:
                input_val.mul_(2.0) 
                input_val.sub_(1.0)
            input_val.mul_(torch.abs(self.alpha))

        ### conv2d operation
        y = nn.functional.conv2d(
            input_val, weight, self.bias, self.stride, self.padding,
            self.dilation, self.groups)
        return y
    
    @property
    def output_size(self):
        ih, iw = self.input_size
        kh, kw = self.kernel_size
        sh, sw = self.stride
        if self.same_padding:
            oh, ow = math.ceil(ih / sh), math.ceil(iw / sw)
        else:
            ph, pw = self.padding
            dh, dw = self.dilation
            oh, ow = out_shape(ih, ph, dh, kh, sh), out_shape(iw, pw, dw, kw, sw)
        return oh, ow
    
    @property
    def comp_cost_loss(self):
        oh, ow = self.output_size
        
        lamda_w = torch.clamp(self.lamda_w, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if self.lamda_w_min is not None:
            lamda_w = torch.clamp(lamda_w, min=self.lamda_w_min)
        act_bits_list = getattr(FLAGS,'act_bits_list',FLAGS.bits_list)
        lamda_a = torch.clamp(self.lamda_a, min(act_bits_list), max(act_bits_list))
        if self.lamda_a_min is not None:
            lamda_a = torch.clamp(lamda_a, min=self.lamda_a_min)
        
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_weight', False):
            lamda_w = lamda_w.view(self.groups, -1, 1)
        #else:
        #    lamda_w = lamda_w.repeat(self.groups, self.out_channels // self.groups, 1)
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_activation', False):
            lamda_a = lamda_a.view(self.groups, 1, -1)
        #else:
        #    lamda_a = lamda_a.repeat(self.groups, 1, self.in_channels // self.groups)
        
        bw_l = lamda_w.floor()
        bw_h = 1 + bw_l
        ba_l = lamda_a.floor()
        ba_h = 1 + ba_l
        
        cc_  = self.kernel_size[0] * self.kernel_size[1] * oh * ow * 1e-9
        #cc_wh_ah = cc_ * ((lamda_w - bw_l) * (lamda_a - ba_l) * bw_h * ba_h).sum()
        #cc_wh_al = cc_ * ((lamda_w - bw_l) * (ba_h - lamda_a) * bw_h * ba_l).sum()
        #cc_wl_ah = cc_ * ((bw_h - lamda_w) * (lamda_a - ba_l) * bw_l * ba_h).sum()
        #cc_wl_al = cc_ * ((bw_h - lamda_w) * (ba_h - lamda_a) * bw_l * ba_l).sum()
        
        #loss =  cc_wh_ah + cc_wh_al + cc_wl_ah + cc_wl_al


        if getattr(FLAGS, 'loss_decoupling', False):
            pass
        else:
            weight_bits_tensor_list = torch.Tensor(FLAGS.bits_list).to(self.weight.device)
            act_bits_tensor_list = torch.Tensor(FLAGS.bits_list).to(self.weight.device)
            if getattr(FLAGS, 'distance_v1', False) or getattr(FLAGS, 'distance_v2', False):
                window_size = getattr(FLAGS, 'window_size', 0)
                if getattr(FLAGS, 'distance_v1', False):
                    mw = 1. / (torch.abs(lamda_w - weight_bits_tensor_list) + self.eps)
                    ma = 1. / (torch.abs(lamda_a - act_bits_tensor_list) + self.eps)
                elif getattr(FLAGS, 'distance_v2', False):
                    mw = torch.relu(torch.Tensor([window_size/2]).to(self.weight.device) - (torch.abs(lamda_w - weight_bits_tensor_list)))
                    ma = torch.relu(torch.Tensor([window_size/2]).to(self.weight.device) - (torch.abs(lamda_a - act_bits_tensor_list)))
                
                #values, indices = torch.topk(mw, window_size, dim=0)
                #mw = mw[indices].view(-1)
                f = mw > 0.005
                mw = mw[f]
                weight_bits_tensor_list = weight_bits_tensor_list[f]
                pw = mw / mw.sum()
                lamda_w = torch.dot(weight_bits_tensor_list, pw)

                #values, indices = torch.topk(ma, window_size, dim=0)
                #ma = ma[indices].view(-1)
                f = ma > 0.005
                ma = ma[f]
                act_bits_tensor_list = act_bits_tensor_list[f]
                pa = ma / ma.sum()
                lamda_a = torch.dot(act_bits_tensor_list, pa)
            #print('lamda_w', lamda_w)
            #print('lamda_a', lamda_a)
            #print('self.in_channels', self.in_channels)
            ##print('self.out_channels', self.out_channels)
            #rint()
        loss = cc_ * lamda_w * lamda_a * self.in_channels * self.out_channels / self.groups
        return loss

    @property
    def model_size_loss(self):
        lamda_w = torch.clamp(self.lamda_w, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if self.lamda_w_min is not None:
            lamda_w = torch.clamp(lamda_w, min=self.lamda_w_min)
        
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_weight', False):
            pass
        else:
            lamda_w = lamda_w.repeat(self.out_channels)

        bw_l = lamda_w.floor()
        bw_h = 1 + bw_l
        
        s_ = self.in_channels * self.kernel_size[0] * self.kernel_size[1] // self.groups / 8e6
        s_wh = s_ * ((lamda_w - bw_l) * bw_h).sum()
        s_wl = s_ * ((bw_h - lamda_w) * bw_l).sum()
        
        loss = s_wh + s_wl
        if self.bias is not None:
            loss += self.out_channels * 4e-6
        return loss

    def bit_discretizing(self):
        self.lamda_w = torch.nn.Parameter(torch.round(self.lamda_w + FLAGS.hard_offset))
        self.lamda_a = torch.nn.Parameter(torch.round(self.lamda_a + FLAGS.hard_offset))
        return 0
      
class QuantizableLinear(nn.Linear):
    def __init__(self, in_features, out_features, bias=True, lamda_w_min=None, lamda_a_min=None, weight_only=False):
        super(QuantizableLinear, self).__init__(
            in_features, out_features, bias=bias)
        self.lamda_w_min = lamda_w_min
        self.lamda_a_min = lamda_a_min
        self.weight_only = weight_only or getattr(FLAGS, 'weight_only', False)
        self.quant = Quantize_k.apply
        self.alpha = nn.Parameter(torch.tensor(10.0))
        init_bit = getattr(FLAGS, 'init_bit', 7.5)
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_weight', False):
            self.lamda_w = nn.Parameter(torch.ones(self.out_features) * init_bit)
        else:
            self.lamda_w = nn.Parameter(torch.tensor(init_bit))
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_activation', False):
            self.lamda_a = nn.Parameter(torch.ones(self.in_features) * init_bit)
        else:
            self.lamda_a = nn.Parameter(torch.tensor(init_bit))
        self.eps = 0.00001
        qtype_list = ['stepsize_aggregation', 'bitwidth_aggregation', 'bitwidth_direct', 
                      'simple_interpolation', 'stepsize_aggregation', 'nlvs_aggregation', 
                      'nlvs_direct']
        self.quant_type = None
        for name in qtype_list:
            if getattr(FLAGS, name, False):
                self.quant_type = name
        if self.quant_type == None:
            self.quant_type = 'original'
        if getattr(FLAGS, 'nlvs_direct', False):
            self.nlvs_w = nn.Parameter(torch.tensor(2 ** init_bit))
            self.nlvs_a = nn.Parameter(torch.tensor(2 ** init_bit))
        if getattr(FLAGS, 'L_value', 0) == 'learned':
            self.gamma = nn.Parameter(torch.tensor(getattr(FLAGS, 'L_init', 1.0)))
        self.sigma = getattr(FLAGS, 'window_size', 2) / 2
        if getattr(FLAGS, 'grad_ema_alpha', False):
            self.ema_lamda_w_grad = nn.Parameter(torch.tensor(0.01), requires_grad=False)
            self.ema_lamda_a_grad = nn.Parameter(torch.tensor(0.01), requires_grad=False)
        
        
    def forward(self, input):
        if getattr(FLAGS, 'full_precision', False):
            return nn.functional.linear(input, self.weight, self.bias)
        if self.lamda_w < min(FLAGS.bits_list):
            self.lamda_w.data = nn.Parameter(torch.tensor(float(min(FLAGS.bits_list)))).to(self.weight.device)
        elif self.lamda_w > max(FLAGS.bits_list):
            self.lamda_w.data = nn.Parameter(torch.tensor(float(max(FLAGS.bits_list)))).to(self.weight.device)

        if self.lamda_a < min(FLAGS.bits_list):
            self.lamda_a.data = nn.Parameter(torch.tensor(float(min(FLAGS.bits_list)))).to(input.device)
        elif self.lamda_a > max(FLAGS.bits_list):
            self.lamda_a.data = nn.Parameter(torch.tensor(float(max(FLAGS.bits_list)))).to(input.device)
        
        lamda_w = self.lamda_w
        lamda_a = self.lamda_a

        #if getattr(FLAGS, 'hard_assignment',False):
        #    lamda_w = torch.round(lamda_w+FLAGS.hard_offset).detach()
        #    lamda_a = torch.round(lamda_a+FLAGS.hard_offset).detach()
        #lamda_w = torch.clamp(lamda_w, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if self.lamda_w_min is not None:
            lamda_w = torch.clamp(lamda_w, min=self.lamda_w_min)
        act_bits_list = getattr(FLAGS,'act_bits_list',FLAGS.bits_list)
        #lamda_a = torch.clamp(self.lamda_a, min(act_bits_list), max(act_bits_list))
        if self.lamda_a_min is not None:
            lamda_a = torch.clamp(lamda_a, min=self.lamda_a_min)
        
        weight_quant_scheme = self.quant_type
        act_quant_scheme = self.quant_type

        ### Weight quantization
        weight = torch.tanh(self.weight) / torch.max(torch.abs(torch.tanh(self.weight)))
        weight.add_(1.0)
        weight.div_(2.0)

        # ------- introduce: general distance-based interpolation 
        # !! Linear layer use 8-bit weight !! # 
        if getattr(FLAGS, 'hard_assignment', True):
            weight = self.quant(weight, lamda_w.detach(), 0, 0.5, 0, 'original')
        elif getattr(FLAGS, 'simple_interpolation', False):
            if self.lamda_w_min == 8:
                weight = self.quant(weight, lamda_w, 0, 0.5, 0, 'simple_interpolation')
            else:
                window_size = getattr(FLAGS, 'window_size', 2)
                print('\n\n\n[Error]This shouldn\'t be printed!!!! ')
                if getattr(FLAGS, 'bitwidth_direct', False):
                    if getattr(FLAGS, 'hard_forward', False):
                        lamda_w = Round.apply(lamda_w)
                    weight = self.quant(weight, lamda_w, 0, 0.5, 0, weight_quant_scheme)
                elif getattr(FLAGS, 'nlvs_direct', False):
                    weight = self.quant(weight, torch.zeros(1), 0, 0.5, self.nlvs_w, weight_quant_scheme)
                    self.lamda_w.data = torch.log2(self.nlvs_w)
                else:
                    weight_bits_tensor_list = torch.Tensor(FLAGS.bits_list).to(weight.device)
                    if getattr(FLAGS, 'distance_v1', False):
                        m = 1. / (torch.abs(lamda_w - weight_bits_tensor_list) + self.eps)
                    elif getattr(FLAGS, 'distance_v2', False):
                        m = torch.relu(torch.Tensor([window_size/2]).to(weight.device) - (torch.abs(lamda_w - weight_bits_tensor_list)))
                    elif getattr(FLAGS, 'distance_gaussian', False):
                        # 1. remake bits tensor using ceil and floor
                        bits_list_new = torch.Tensor(
                                        [torch.floor(lamda_w-2),
                                        torch.floor(lamda_w-1), 
                                        torch.floor(lamda_w), 
                                        torch.ceil(lamda_w),
                                        torch.ceil(lamda_w+1),
                                        torch.ceil(lamda_w+2)]).to(weight.device)
                        # 2. filter bits_list_new by magnitude
                        bits_list_new = bits_list_new[bits_list_new > min(FLAGS.bits_list)]
                        weight_bits_tensor_list = bits_list_new[bits_list_new < max(FLAGS.bits_list)]
                        m = torch.exp(-torch.pow(weight_bits_tensor_list-lamda_w, 2)/2*(1/self.sigma)*(1/self.sigma))
                    else:
                        m = torch.zeros_like(weight_bits_tensor_list)
                    L = getattr(FLAGS, 'L_value', 1)
                    if L == 'learned':
                        m = torch.pow(m, self.gamma)
                    elif L == 'softmax':
                        tau = getattr(FLAGS, 'tau', 0)
                        m = F.softmax(m / tau, dim=0)
                    else:
                        m = torch.pow(m, L)
                    f = m > 0.05
                    m = m[f]
                    weight_bits_tensor_list = weight_bits_tensor_list[f]
                    p = m / m.sum(dim=0, keepdim=True)
                    ##################################################
                    if getattr(FLAGS, 'stepsize_aggregation', False):
                        # lamda_w is 8
                        #stepsize_tensor_list = 1/(torch.pow(2, weight_bits_tensor_list)-1)
                        #s = torch.sum(p * stepsize_tensor_list)
                        s = 1/(torch.pow(2, lamda_w)-1)
                        weight = self.quant(weight, torch.zeros(1), 1, 0, s, weight_quant_scheme)
                    elif getattr(FLAGS, 'nlvs_aggregation', False):
                        # lambda_w is 8
                        #a = torch.dot(p, (torch.pow(2, weight_bits_tensor_list)-1))
                        a = torch.pow(2, lamda_w)-1
                        input_val = self.quant(weight, torch.zeros(1), 1, 0, a, weight_quant_scheme)
                    elif getattr(FLAGS, 'bitwidth_aggregation', False):
                        interpolated_bit = sum([p[i] * weight_bits_tensor_list[i] for i in range(len(p))])
                        weight = self.quant(weight, interpolated_bit, 0, 0.5, 0, weight_quant_scheme)
                    else:
                        weight_list = []
                        for i, bit in enumerate(weight_bits_tensor_list):
                            weight_list.append(p[i].view(-1, 1) * self.quant(weight, bit, 0, 0.5, 0, weight_quant_scheme))
                        weight = torch.stack(weight_list).sum(dim=0)
            # ----------------------------------------------
        else:
            p_l = 1 + torch.floor(lamda_w) - lamda_w
            p_h = 1 - p_l
            if not getattr(FLAGS, 'hard_assignment', False) and getattr(FLAGS,'gumbel_softmax',False):
                logits = torch.Tensor([torch.log(p_l)+ self.eps, torch.log(p_h) + self.eps]).view(1,2).cuda()
                one_hot = gumbel_softmax(logits, getattr(FLAGS, 'temperature',1.0))
                p_l = one_hot[0,0]
                p_h = one_hot[0,1]
            weight = p_h.view(-1,1) * self.quant(weight, torch.ceil(lamda_w), 0, 0.5, 0, weight_quant_scheme) \
                + p_l.view(-1,1) * self.quant(weight, torch.floor(lamda_w), 0, 0.5, 0, weight_quant_scheme)
        weight.mul_(2.0)
        weight.sub_(1.0)
        if getattr(FLAGS, 'rescale', True):
            rescale_type = getattr(FLAGS, 'rescale_type', 'constant')
            if rescale_type == 'stddev':
                weight_scale = torch.std(self.weight.detach())
            elif rescale_type == 'constant':
                weight_scale = 1.0 / (self.out_features) ** 0.5
            else:
                raise NotImplementedError
            weight_scale /= torch.std(weight.detach())
            if self.training:
                weight.mul_(weight_scale)
        if self.bias is not None:
            bias = self.bias
            if getattr(FLAGS, 'rescale', True) and not self.training:
                bias = bias / weight_scale
        else:
            bias = self.bias

        ### Activation quantization
        if self.weight_only:
            input_val = input
        else:
            input_val = torch.where(input < torch.abs(self.alpha), input, torch.abs(self.alpha).type(input.type()))
            input_val.div_(torch.abs(self.alpha))
            # ------- introduce: general distance-based interpolation 
            if getattr(FLAGS, 'hard_assignment', False):
                input_val = self.quant(input_val, lamda_a.detach(), 1, 0, 0, 'original')
            elif getattr(FLAGS, 'simple_interpolation', False):
                if getattr(FLAGS, 'bitwidth_direct', False):
                    if getattr(FLAGS, 'hard_forward', False):
                        lamda_a = Round.apply(lamda_a)
                    input_val = self.quant(input_val, lamda_a, 1, 0, 0, act_quant_scheme)
                elif getattr(FLAGS, 'nlvs_direct', False):
                    input_val = self.quant(input_val, torch.zeros(1), 1, 0, self.nlvs_a, weight_quant_scheme)
                    self.lamda_a.data = torch.log2(self.nlvs_a)
                else:
                    window_size = getattr(FLAGS, 'window_size', 2)
                    act_bits_tensor_list = torch.Tensor(act_bits_list).to(input_val.device)
                    if getattr(FLAGS, 'distance_v1', False):
                        m = 1. / (torch.abs(lamda_a - act_bits_tensor_list) + self.eps)
                    elif getattr(FLAGS, 'distance_v2', False):
                        m = torch.relu(torch.Tensor([window_size/2]).to(input_val.device) - (torch.abs(lamda_a - act_bits_tensor_list)))
                    elif getattr(FLAGS, 'distance_gaussian', False):
                        bits_list_new = torch.Tensor(
                                        [torch.floor(lamda_a-2),
                                        torch.floor(lamda_a-1), 
                                        torch.floor(lamda_a), 
                                        torch.ceil(lamda_a),
                                        torch.ceil(lamda_a+1),
                                        torch.ceil(lamda_a+2)]).to(input_val.device)
                        bits_list_new = bits_list_new[bits_list_new > min(FLAGS.bits_list)]
                        act_bits_tensor_list = bits_list_new[bits_list_new < max(FLAGS.bits_list)]
                        m = torch.exp(-torch.pow(act_bits_tensor_list-lamda_a, 2)/2*(1/self.sigma)*(1/self.sigma))
                    else:
                        m = torch.zeros_like(act_bits_tensor_list)
                    L = getattr(FLAGS, 'L_value', 1)
                    if L == 'learned':
                        m = torch.pow(m, self.gamma)
                    elif L == 'softmax':
                        tau = getattr(FLAGS, 'tau', 0)
                        m = F.softmax(m / tau, dim=0)
                    else:
                        m = torch.pow(m, L)
                    f = m > 0.05
                    m = m[f]
                    act_bits_tensor_list = act_bits_tensor_list[f]
                    p = m / m.sum(dim=0)
                    ####################################################
                    if self.lamda_a_min == 8:
                        input_val = self.quant(input_val, lamda_a, 1, 0, 0, 'simple_interpolation')
                    elif getattr(FLAGS, 'stepsize_aggregation', False):
                        stepsize_tensor_list = 1/(torch.pow(2, act_bits_tensor_list)-1)
                        s = torch.dot(p.view(-1), stepsize_tensor_list)
                        input_val = self.quant(input_val, torch.zeros(1), 1, 0, s, act_quant_scheme)
                    elif getattr(FLAGS, 'nlvs_aggregation', False):
                        a = torch.dot(p.view(-1), torch.pow(2, act_bits_tensor_list)-1)
                        input_val = self.quant(input_val, torch.zeros(1), 1, 0, a, act_quant_scheme)
                    elif getattr(FLAGS, 'bitwidth_aggregation', False):
                        interpolated_bit = sum([p[i] * act_bits_tensor_list[i] for i in range(len(p))])
                        input_val = self.quant(input_val, interpolated_bit, 1, 0, 0, act_quant_scheme)
                    
                    else:
                        input_temp = 0
                        for i, bit in enumerate(act_bits_tensor_list):
                            input_temp += p[i] * self.quant(input_val, bit, 1, 0, 0, act_quant_scheme)
                        input_val = input_temp
                # ----------------------------------------------
            else:
                p_a_l = 1 + torch.floor(lamda_a) - lamda_a
                p_a_h = 1 - p_a_l
                if not getattr(FLAGS, 'hard_assignment', False) and getattr(FLAGS,'gumbel_softmax',False):
                    logits = torch.Tensor([torch.log(p_a_l)+ self.eps, torch.log(p_a_h) + self.eps]).view(1,2).cuda()
                    one_hot = gumbel_softmax(logits, getattr(FLAGS, 'temperature',1.0))
                    p_a_l = one_hot[0,0]
                    p_a_h = one_hot[0,1]
                input_val = p_a_h * self.quant(input_val, torch.ceil(lamda_a), 1, 0, 0, act_quant_scheme) \
                    + p_a_l * self.quant(input_val, torch.floor(lamda_a), 1, 0, 0, act_quant_scheme)
            try:
                input_val.mul_(torch.abs(self.alpha))
            except:
                print('act_bits_tensor_list:', act_bits_tensor_list)
                print('lamda_a',lamda_a)
                exit()
        
        ### Linear operation
        return nn.functional.linear(input_val, weight, bias)
    
    @property
    def comp_cost_loss(self):
        lamda_w = torch.clamp(self.lamda_w, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if getattr(FLAGS, 'nlvs_direct', False):
            lamda_w = torch.log2(self.nlvs_w)
            lamda_w = torch.clamp(lamda_w, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if self.lamda_w_min is not None:
            lamda_w = torch.clamp(lamda_w, min=self.lamda_w_min)

        act_bits_list = getattr(FLAGS,'act_bits_list',FLAGS.bits_list)
        lamda_a = torch.clamp(self.lamda_a, min(act_bits_list), max(act_bits_list))
        if getattr(FLAGS, 'nlvs_direct', False):
            lamda_a = torch.log2(self.nlvs_a)
            lamda_a = torch.clamp(lamda_a, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if self.lamda_a_min is not None:
            lamda_a = torch.clamp(lamda_a, min=self.lamda_a_min)
        
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_weight', False):
            lamda_w = lamda_w.view(-1, 1)
        #else:
        #    lamda_w = lamda_w.repeat(self.out_features, 1)
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_activation', False):
            lamda_a = lamda_a.view(1, -1)
        #else:
        #    lamda_a = lamda_a.repeat(1, self.in_features)

        bw_l = lamda_w.floor()
        bw_h = 1 + bw_l
        ba_l = lamda_a.floor()
        ba_h = 1 + ba_l
        
        cc_ = 1e-9
        if getattr(FLAGS, 'loss_decoupling', False):
            pass
        else: 
            weight_bits_tensor_list = torch.Tensor(FLAGS.bits_list).to(self.weight.device)
            act_bits_tensor_list = torch.Tensor(FLAGS.bits_list).to(self.weight.device)
            if getattr(FLAGS, 'distance_v1', False) or getattr(FLAGS, 'distance_v2', False):
                window_size = getattr(FLAGS, 'window_size', 0)
                if getattr(FLAGS, 'distance_v1', False):
                    mw = 1. / (torch.abs(lamda_w.view(1, -1) - weight_bits_tensor_list.view(-1, 1)) + self.eps)
                    ma = 1. / (torch.abs(lamda_a.view(1, -1) - act_bits_tensor_list.view(-1, 1)) + self.eps)
                elif getattr(FLAGS, 'distance_v2', False):
                    mw = torch.relu(torch.Tensor([window_size/2]).to(self.weight.device) - (torch.abs(lamda_w - weight_bits_tensor_list)))
                    ma = torch.relu(torch.Tensor([window_size/2]).to(self.weight.device) - (torch.abs(lamda_a - act_bits_tensor_list)))
                f = mw > 0
                mw = mw[f]
                weight_bits_tensor_list = weight_bits_tensor_list[f]
                pw = mw / mw.sum()
                lamda_w = torch.dot(weight_bits_tensor_list, pw)

                f = ma > 0
                ma = ma[f]
                act_bits_tensor_list = act_bits_tensor_list[f]
                pa = ma / ma.sum()
                lamda_a = torch.dot(act_bits_tensor_list, pa)

        loss = cc_ * lamda_w * lamda_a * self.in_features * self.out_features / self.groups
        return loss

    @property
    def model_size_loss(self):
        lamda_w = torch.clamp(self.lamda_w, min(FLAGS.bits_list), max(FLAGS.bits_list))
        if self.lamda_w_min is not None:
            lamda_w = torch.clamp(lamda_w, min=self.lamda_w_min)
        
        if getattr(FLAGS, 'per_channel', False) or getattr(FLAGS, 'per_channel_weight', False):
            pass
        else:
            lamda_w = lamda_w.repeat(self.out_features)

        bw_l = lamda_w.floor()
        bw_h = 1 + bw_l
        
        s_ = self.in_features / 8e6
        s_wh = s_ * ((lamda_w - bw_l) * bw_h).sum()
        s_wl = s_ * ((bw_h - lamda_w) * bw_l).sum()
        
        loss = s_wh + s_wl
        if self.bias is not None:
            loss += self.out_features * 4e-6
        return loss

    def bit_discretizing(self):
        self.lamda_w = torch.nn.Parameter(torch.round(self.lamda_w + FLAGS.hard_offset))
        self.lamda_a = torch.nn.Parameter(torch.round(self.lamda_a + FLAGS.hard_offset))
        return 0

class MaxPool2d(nn.MaxPool2d):
    def __init__(self, kernel_size, stride=None, padding=0,
                 dilation=1, input_size=(0, 0)):
        super(MaxPool2d, self).__init__(kernel_size, stride, padding,
                                        dilation)
        self.input_size = input_size
    
    @property
    def output_size(self):
        ih, iw = self.input_size
        kh, kw = _pair(self.kernel_size)
        sh, sw = _pair(self.stride)
        ph, pw = _pair(self.padding)
        dh, dw = _pair(self.dilation)
        oh, ow = out_shape(ih, ph, dh, kh, sh), out_shape(iw, pw, dw, kw, sw)
        return oh, ow
