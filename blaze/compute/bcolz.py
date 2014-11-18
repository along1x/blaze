from __future__ import absolute_import, division, print_function

from toolz import curry, concat
from multipledispatch import MDNotImplementedError

from ..expr import (Selection, Head, Field, Projection, ReLabel, ElemWise,
        Arithmetic, Broadcast, Symbol)
from ..expr import Label, Distinct, By, Reduction, Like, Slice
from ..expr import std, var, count, mean, nunique, sum
from ..expr import eval_str, Expr, nelements
from ..expr import path
from ..expr.optimize import lean_projection
from ..expr.split import split
from ..partition import partitions, partition_get
from .core import compute
from ..utils import available_memory

from collections import Iterator, Iterable
import datashape
import bcolz
import math
import numpy as np
import pandas as pd
from .chunks import ChunkIndexable


from ..compatibility import builtins
from ..dispatch import dispatch
from ..api import into

__all__ = ['bcolz']

COMFORTABLE_MEMORY_SIZE = 1e9


@dispatch(Expr, (bcolz.ctable, bcolz.carray))
def pre_compute(expr, data, scope=None):
    return data


@dispatch((bcolz.carray, bcolz.ctable))
def discover(data):
    return datashape.from_numpy(data.shape, data.dtype)

Cheap = (Head, ElemWise, Selection, Distinct, Symbol)

@dispatch(Head, (bcolz.ctable, bcolz.carray))
def compute_down(expr, data, **kwargs):
    """ Cheap and simple computation in simple case

    If we're given a head and the entire expression is cheap to do (e.g.
    elemwises, selections, ...) then compute on data directly, without
    parallelism"""
    leaf = expr._leaves()[0]
    if all(isinstance(e, Cheap) for e in path(expr, leaf)):
        return compute(expr, {leaf: into(Iterator, data)}, **kwargs)
    else:
        raise MDNotImplementedError()


@dispatch(Selection, bcolz.ctable)
def compute_up(expr, data, **kwargs):
    if data.nbytes < available_memory() / 4:
        return compute_up(expr, data[:], **kwargs)
    s = eval_str(expr.predicate._expr)
    try:
        return data.where(s)
    except (NotImplementedError, NameError, AttributeError):
        # numexpr may not be able to handle the predicate
        return compute_up(expr, into(Iterator, data), **kwargs)


@dispatch(Selection, bcolz.ctable)
def compute_up(expr, data, **kwargs):
    if data.nbytes < available_memory() / 4:
        return compute_up(expr, data[:], **kwargs)
    return compute_up(expr, into(Iterator, data), **kwargs)


@dispatch(Head, (bcolz.carray, bcolz.ctable))
def compute_up(expr, data, **kwargs):
    return data[:expr.n]


@dispatch(Field, bcolz.ctable)
def compute_up(expr, data, **kwargs):
    return data[expr._name]


@dispatch(Projection, bcolz.ctable)
def compute_up(expr, data, **kwargs):
    return data[expr.fields]


@dispatch(sum, (bcolz.carray, bcolz.ctable))
def compute_up(expr, data, **kwargs):
    result = data.sum()
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch(count, (bcolz.ctable, bcolz.carray))
def compute_up(expr, data, **kwargs):
    result = len(data)
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch(mean, bcolz.carray)
def compute_up(expr, ba, **kwargs):
    result = ba.sum() / ba.len
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch(var, bcolz.carray)
def compute_up(expr, ba, chunksize=2**20, **kwargs):
    n = ba.len
    E_X_2 = builtins.sum((chunk * chunk).sum() for chunk in chunks(ba))
    E_X = float(ba.sum())
    result = (E_X_2 - (E_X * E_X) / n) / (n - expr.unbiased)
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch(std, bcolz.carray)
def compute_up(expr, ba, **kwargs):
    result = compute_up(expr._child.var(unbiased=expr.unbiased), ba, **kwargs)
    result = math.sqrt(result)
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch((ReLabel, Label), (bcolz.carray, bcolz.ctable))
def compute_up(expr, b, **kwargs):
    raise NotImplementedError()


@dispatch((Arithmetic, Broadcast, ElemWise, Distinct, By, nunique, Like),
          (bcolz.carray, bcolz.ctable))
def compute_up(expr, data, **kwargs):
    if data.nbytes < available_memory() / 4:
        return compute_up(expr, data[:], **kwargs)
    return compute_up(expr, into(Iterator, data), **kwargs)


@dispatch(nunique, bcolz.carray)
def compute_up(expr, data, **kwargs):
    result = len(set(data))
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch(Reduction, (bcolz.carray, bcolz.ctable))
def compute_up(expr, data, **kwargs):
    if data.nbytes < available_memory() / 4:
        result = compute_up(expr, data[:], **kwargs)
    result = compute_up(expr, ChunkIndexable(data), **kwargs)
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch(Slice, (bcolz.carray, bcolz.ctable))
def compute_up(expr, x, **kwargs):
    return x[expr.index]


@dispatch(nelements, (bcolz.carray, bcolz.ctable))
def compute_up(expr, x, **kwargs):
    result = compute_up.dispatch(type(expr), np.ndarray)(expr, x, **kwargs)
    if expr.keepdims:
        result = np.array([result])
    return result


@dispatch((bcolz.carray, bcolz.ctable))
def chunks(b, chunksize=2**15):
    start = 0
    n = b.len
    while start < n:
        yield b[start:start + chunksize]
        start += chunksize


@dispatch((bcolz.carray, bcolz.ctable), int)
def get_chunk(b, i, chunksize=2**15):
    start = chunksize * i
    stop = chunksize * (i + 1)
    return b[start:stop]


@dispatch(Expr, (bcolz.ctable, bcolz.carray))
def optimize(expr, _):
    return lean_projection(expr)


def compute_chunk(source, chunk, chunk_expr, data_index):
    part = source[data_index]
    return compute(chunk_expr, {chunk: part})


@dispatch(Expr, (bcolz.carray, bcolz.ctable))
def compute_down(expr, data, chunksize=2**20, map=map, **kwargs):
    leaf = expr._leaves()[0]

    chunk = Symbol('chunk', chunksize * leaf.schema)
    (chunk, chunk_expr), (agg, agg_expr) = split(leaf, expr, chunk=chunk)

    data_parts = partitions(data, chunksize=(chunksize,))

    parts = map(curry(compute_chunk, data, chunk, chunk_expr),
                       data_parts)

    if isinstance(parts[0], np.ndarray):
        intermediate = np.concatenate(parts)
    elif isinstance(parts[0], pd.DataFrame):
        intermediate = pd.concat(parts)
    elif isinstance(parts[0], Iterable):
        intermediate = list(concat(parts))

    return compute(agg_expr, {agg: intermediate})
