# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Function to compute Monte Carlo NNGP and NTK estimates.

This module contains a function `monte_carlo_kernel_fn` that allow to compute
Monte Carlo estimates of NNGP and NTK kernels of arbitrary functions. For more
details on how individual samples are computed, refer to `utils/empirical.py`.

Note that the `monte_carlo_kernel_fn` accepts arguments like `batch_size`,
`device_count`, and `store_on_device`, and is appropriately batched /
parallelized. You don't need to apply the `nt.batch` or `jax.jit` decorators to
it. Further, you do not need to apply `jax.jit` to the input `apply_fn`
function, as the resulting empirical kernel function is JITted internally.
"""


from functools import partial
import operator
from typing import Union, Tuple, Generator, Set, Iterable, Optional

from jax.tree_util import tree_map
from jax.tree_util import tree_multimap
from neural_tangents.utils import batch
from neural_tangents.utils import empirical
from neural_tangents.utils import utils
from neural_tangents.utils.typing import PRNGKey, InitFn, ApplyFn, MonteCarloKernelFn, Axes, Get, EmpiricalKernelFn, PyTree

import tensorflow as tf
from tensorflow.python.ops import numpy_ops as np
from tensorflow.python.ops import stateless_random_ops as random


def _sample_once_kernel_fn(kernel_fn: EmpiricalKernelFn,
                           init_fn: InitFn,
                           batch_size: int = 0,
                           device_count: int = -1,
                           store_on_device: bool = True):
  @partial(batch.batch,
           batch_size=batch_size,
           device_count=device_count,
           store_on_device=store_on_device)
  def kernel_fn_sample_once(
      x1: np.ndarray,
      x2: Optional[np.ndarray],
      key: PRNGKey,
      get: Get,
      **apply_fn_kwargs):
    keys = random.split(key, 2)
    init_key, dropout_key = keys[0], keys[1]
    _, params = init_fn(init_key, x1.shape)
    return kernel_fn(x1, x2, get, params, rng=dropout_key, **apply_fn_kwargs)
  return kernel_fn_sample_once


def _sample_many_kernel_fn(
    kernel_fn_sample_once,
    key: PRNGKey,
    n_samples: Set[int],
    get_generator: bool):
  def normalize(sample: PyTree, n: int) -> PyTree:
    return tree_map(lambda sample: sample / n, sample)

  def get_samples(
      x1: np.ndarray,
      x2: Optional[np.ndarray],
      get: Get,
      **apply_fn_kwargs):
    _key = key
    ker_sampled = None
    for n in range(1, max(n_samples) + 1):
      keys = random.split(_key)
      _key, split = keys[0], keys[1]
      one_sample = kernel_fn_sample_once(x1, x2, split, get, **apply_fn_kwargs)
      if ker_sampled is None:
        ker_sampled = one_sample
      else:
        ker_sampled = tree_multimap(operator.add, ker_sampled, one_sample)
      yield n, ker_sampled

  if get_generator:
    @utils.get_namedtuple('MonteCarloKernel')
    def get_sampled_kernel(
        x1: np.ndarray,
        x2: np.ndarray,
        get: Get = None,
        **apply_fn_kwargs
    ) -> Generator[Union[np.ndarray, Tuple[np.ndarray, ...]], None, None]:
      for n, sample in get_samples(x1, x2, get, **apply_fn_kwargs):
        if n in n_samples:
          yield normalize(sample, n)
  else:
    @utils.get_namedtuple('MonteCarloKernel')
    def get_sampled_kernel(
        x1: np.ndarray,
        x2: np.ndarray,
        get: Get = None,
        **apply_fn_kwargs
    ) -> Union[np.ndarray, Tuple[np.ndarray, ...]]:
      for n, sample in get_samples(x1, x2, get, **apply_fn_kwargs):
        pass
      return normalize(sample, n)

  return get_sampled_kernel


def monte_carlo_kernel_fn(
    init_fn: InitFn,
    apply_fn: ApplyFn,
    key: PRNGKey,
    n_samples: Union[int, Iterable[int]],
    batch_size: int = 0,
    device_count: int = -1,
    store_on_device: bool = True,
    trace_axes: Axes = (-1,),
    diagonal_axes: Axes = ()
    ) -> MonteCarloKernelFn:
  """Return a Monte Carlo sampler of NTK and NNGP kernels of a given function.

  Note that the returned function is appropriately batched / parallelized. You
  don't need to apply the `nt.batch` or `jax.jit` decorators  to it. Further,
  you do not need to apply `jax.jit` to the input `apply_fn` function, as the
  resulting empirical kernel function is JITted internally.

  Args:
    init_fn:
      a function initializing parameters of the neural network. From
      `jax.experimental.stax`: "takes an rng key and an input shape and returns
      an `(output_shape, params)` pair".
    apply_fn:
      a function computing the output of the neural network.
      From `jax.experimental.stax`: "takes params, inputs, and an rng key and
      applies the layer".
    key:
      RNG (`jax.random.PRNGKey`) for sampling random networks. Must have
      shape `(2,)`.
    n_samples:
      number of Monte Carlo samples. Can be either an integer or an
      iterable of integers at which the resulting generator will yield
      estimates. Example: use `n_samples=[2**k for k in range(10)]` for the
      generator to yield estimates using 1, 2, 4, ..., 512 Monte Carlo samples.
    batch_size: an integer making the kernel computed in batches of `x1` and
      `x2` of this size. `0` means computing the whole kernel. Must divide
      `x1.shape[0]` and `x2.shape[0]`.
    device_count:
      an integer making the kernel be computed in parallel across
      this number of devices (e.g. GPUs or TPU cores). `-1` means use all
      available devices. `0` means compute on a single device sequentially. If
      not `0`, must divide `x1.shape[0]`.
    store_on_device:
      a boolean, indicating whether to store the resulting
      kernel on the device (e.g. GPU or TPU), or in the CPU RAM, where larger
      kernels may fit.
    trace_axes:
      output axes to trace the output kernel over, i.e. compute only the trace
      of the covariance along the respective pair of axes (one pair for each
      axis in `trace_axes`). This allows to save space and compute if you are
      only interested in the respective trace, but also improve approximation
      accuracy if you know that covariance along these pairs of axes converges
      to a `constant * identity matrix` in the limit of interest (e.g.
      infinite width or infinite `n_samples`). A common use case is the channel
      / feature / logit axis, since activation slices along such axis are i.i.d.
      and the respective covariance along the respective pair of axes indeed
      converges to a constant-diagonal matrix in the infinite width or infinite
      `n_samples` limit.
      Also related to "contracting dimensions" in XLA terms.
      (https://www.tensorflow.org/xla/operation_semantics#dotgeneral)
    diagonal_axes:
      output axes to diagonalize the output kernel over, i.e. compute only the
      diagonal of the covariance along the respective pair of axes (one pair for
      each axis in `diagonal_axes`). This allows to save space and compute, if
      off-diagonal values along these axes are not needed, but also improve
      approximation accuracy if their limiting value is known theoretically,
      e.g. if they vanish in the limit of interest (e.g. infinite
      width or infinite `n_samples`). If you further know that on-diagonal
      values converge to the same constant in your limit of interest, you should
      specify these axes in `trace_axes` instead, to save even more compute and
      gain even more accuracy. A common use case is computing the variance
      (instead of covariance) along certain axes.
      Also related to "batch dimensions" in XLA terms.
      (https://www.tensorflow.org/xla/operation_semantics#dotgeneral)

  Returns:
    If `n_samples` is an integer, returns a function of signature
    `kernel_fn(x1, x2, get)` that returns an MC estimation of the kernel using
    `n_samples`. If `n_samples` is a collection of integers,
    `kernel_fn(x1, x2, get)` returns a generator that yields estimates using
    `n` samples for `n in n_samples`.

  Example:
    >>> from jax import random
    >>> import neural_tangents as nt
    >>> from neural_tangents import stax
    >>>
    >>> key1, key2 = random.split(random.PRNGKey(1), 2)
    >>> x_train = random.normal(key1, (20, 32, 32, 3))
    >>> y_train = random.uniform(key1, (20, 10))
    >>> x_test = random.normal(key2, (5, 32, 32, 3))
    >>>
    >>> init_fn, apply_fn, _ = stax.serial(
    >>>     stax.Conv(128, (3, 3)),
    >>>     stax.Relu(),
    >>>     stax.Conv(256, (3, 3)),
    >>>     stax.Relu(),
    >>>     stax.Conv(512, (3, 3)),
    >>>     stax.Flatten(),
    >>>     stax.Dense(10)
    >>> )
    >>>
    >>> n_samples = 200
    >>> kernel_fn = nt.monte_carlo_kernel_fn(init_fn, apply_fn, key1, n_samples)
    >>> kernel = kernel_fn(x_train, x_test, get=('nngp', 'ntk'))
    >>> # `kernel` is a tuple of NNGP and NTK MC estimate using `n_samples`.
    >>>
    >>> n_samples = [1, 10, 100, 1000]
    >>> kernel_fn_generator = nt.monte_carlo_kernel_fn(init_fn, apply_fn, key1,
    >>>                                                n_samples)
    >>> kernel_samples = kernel_fn_generator(x_train, x_test,
    >>>                                      get=('nngp', 'ntk'))
    >>> for n, kernel in zip(n_samples, kernel_samples):
    >>>   print(n, kernel)
    >>>   # `kernel` is a tuple of NNGP and NTK MC estimate using `n` samples.
  """
  kernel_fn = empirical.empirical_kernel_fn(apply_fn,
                                            trace_axes=trace_axes,
                                            diagonal_axes=diagonal_axes)

  kernel_fn_sample_once = _sample_once_kernel_fn(kernel_fn,
                                                 init_fn,
                                                 batch_size,
                                                 device_count,
                                                 store_on_device)

  n_samples, get_generator = _canonicalize_n_samples(n_samples)
  kernel_fn = _sample_many_kernel_fn(kernel_fn_sample_once,
                                     key,
                                     n_samples,
                                     get_generator)
  return kernel_fn


def _canonicalize_n_samples(
    n_samples: Union[int, Iterable[int]]) -> Tuple[Set[int], bool]:
  get_generator = True
  if isinstance(n_samples, int):
    get_generator = False
    n_samples = (n_samples,)

  if hasattr(n_samples, '__iter__'):
    n_samples = set(n_samples)

    if not all(isinstance(n, int) for n in n_samples):
      raise ValueError(f'`n_samples` must contain only integers, '
                       f'got {n_samples}.')

    if any(n <= 0 for n in n_samples):
      raise ValueError(f'`n_samples` must be positive, got {n_samples}.')

  else:
    raise TypeError(f'`n_samples` must be either an integer of a set of '
                    f'integers, got {type(n_samples)}.')
  return n_samples, get_generator
