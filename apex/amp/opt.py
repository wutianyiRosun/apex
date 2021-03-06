import contextlib
import logging
import warnings

from .scaler import LossScaler, iter_params

import numpy as np

class OptimWrapper(object):
    def __init__(self, optimizer, amp_handle, num_loss):
        self._optimizer = optimizer
        self._amp_handle = amp_handle
        self._num_loss = num_loss
        self._loss_idx = 0
        self._skip_next = [False] * num_loss
        self._loss_scaler = [LossScaler() for _ in range(num_loss)]

    @contextlib.contextmanager
    def scale_loss(self, loss):
        if not self._amp_handle.is_active():
            yield loss
            return

        loss_backward = loss.backward
        def warning_wrapper():
            warnings.warn("You called .backward() on the unscaled loss "
                          "inside a scale_loss block. This is almost "
                          "certainly an error.", stacklevel=2)
            loss_backward()
        loss.backward = warning_wrapper

        # When there are multiple losses per-optimizer, we need
        # to save out current grad accumulation, since we won't be
        # able to unscale this particulare loss once the grads are
        # all mixed together.
        cached_grads = []
        if self._loss_idx > 0:
            for p in iter_params(self._optimizer.param_groups):
                if p.grad is not None:
                    cached_grads.append(p.grad.data.detach().clone())
                else:
                    cached_grads.append(None)
            self._optimizer.zero_grad()
        
        loss_scale = self._cur_loss_scaler().loss_scale()
        yield loss * loss_scale
        loss.backward = loss_backward

        self._skip_next[self._loss_idx] = self._cur_loss_scaler().unscale_and_update(
            self._optimizer.param_groups, loss_scale)
        self._loss_idx += 1

        if len(cached_grads) > 0:
            for p, cached_grad in zip(iter_params(self._optimizer.param_groups),
                                      cached_grads):
                if cached_grad is not None:
                    p.grad.data.add_(cached_grad)
            cached_grads = []

    def _cur_loss_scaler(self):
        assert 0 <= self._loss_idx < self._num_loss
        return self._loss_scaler[self._loss_idx]

    def step(self, closure=None):
        if not self._amp_handle.is_active():
            return self._optimizer.step(closure=closure)

        self._loss_idx = 0

        for group in self._optimizer.param_groups:
            for p in group['params']:
                self._amp_handle.remove_cache(p)

        if closure is not None:
            raise NotImplementedError(
                'The `closure` argument is unsupported by the amp ' +
                'optimizer wrapper.')
        if any(self._skip_next):
            logging.info('Gradient overflow, skipping update')
            self._skip_next = [False] * self._num_loss
        else:
            return self._optimizer.step(closure=closure)

    # Forward any attribute lookups
    def __getattr__(self, attr):
        return getattr(self._optimizer, attr)

    # Forward all torch.optim.Optimizer methods
    def __getstate__(self):
        return self._optimizer.__getstate__()

    def __setstate__(self):
        return self._optimizer.__setstate__()

    def __repr__(self):
        return self._optimizer.__repr__()

    def state_dict(self):
        return self._optimizer.state_dict()

    def load_state_dict(self, state_dict):
        return self._optimizer.load_state_dict(state_dict)

    def zero_grad(self):
        return self._optimizer.zero_grad()

    def add_param_group(self, param_group):
        return self._optimizer.add_param_group(param_group)
