"""
Multilayer Perceptron
"""
__authors__ = "Ian Goodfellow"
__copyright__ = "Copyright 2012-2013, Universite de Montreal"
__credits__ = ["Ian Goodfellow"]
__license__ = "3-clause BSD"
__maintainer__ = "Ian Goodfellow"

from collections import OrderedDict
import numpy as np
import warnings

from theano import config
from theano.gof.op import get_debug_values
from theano.printing import Print
from theano.sandbox.rng_mrg import MRG_RandomStreams
import theano.tensor as T

from pylearn2.costs.cost import Cost
from pylearn2.expr.probabilistic_max_pooling import max_pool_channels
from pylearn2.linear import conv2d
try:
    from pylearn2.linear import conv2d_c01b
except ImportError:
    warnings.warn("Couldn't import Alex-style convolution, probably because you don't have a GPU"
            "Some stuff might be broken.")
from pylearn2.linear.matrixmul import MatrixMul
from pylearn2.models.model import Model
from pylearn2.space import Conv2DSpace
from pylearn2.space import Space
from pylearn2.space import VectorSpace
from pylearn2.utils import function
from pylearn2.utils import safe_izip
from pylearn2.utils import sharedX
from pylearn2.models.mlp import max_pool
from pylearn2.sandbox.cuda_convnet.pool import max_pool_c01b
from pylearn2.models.mlp import Layer
from pylearn2.monitor import Monitor

class Adaptive(Layer):
    """
        WRITEME
    """

    def __init__(self,
                 dim,
                 layer_name,
                 irange = None,
                 istdev = None,
                 sparse_init = None,
                 sparse_stdev = 1.,
                 include_prob = 1.0,
                 init_bias = None,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 switch_lr_scale = None,
                 mask_weights = None,
                 left_slope = 0.0,
                 copy_input = 0,
                 max_row_norm = None,
                 max_col_norm = None,
                 use_bias = True):
        """

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

            """

        if use_bias and init_bias is None:
            init_bias = 0.

        self.__dict__.update(locals())
        del self.self

        self.switch = sharedX( np.ones((self.dim,)) * 0.5, name = layer_name+'_switch')

        if use_bias:
            self.b = sharedX( np.zeros((self.dim,)) + init_bias, name = layer_name + '_b')
        else:
            assert b_lr_scale is None
            init_bias is None

    def get_lr_scalers(self):

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.use_bias and self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        if self.switch_lr_scale is not None:
            rval[self.switch] = self.switch_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        if isinstance(space, VectorSpace):
            self.requires_reformat = False
            self.input_dim = space.dim
        else:
            self.requires_reformat = True
            self.input_dim = space.get_total_dimension()
            self.desired_space = VectorSpace(self.input_dim)

        self.output_space = VectorSpace(self.dim + self.copy_input * self.input_dim)

        rng = self.mlp.rng
        if self.irange is not None:
            assert self.istdev is None
            assert self.sparse_init is None
            W = rng.uniform(-self.irange,
                            self.irange,
                            (self.input_dim, self.dim)) * \
                (rng.uniform(0.,1., (self.input_dim, self.dim))
                 < self.include_prob)
        elif self.istdev is not None:
            assert self.sparse_init is None
            W = rng.randn(self.input_dim, self.dim) * self.istdev
        else:
            assert self.sparse_init is not None
            W = np.zeros((self.input_dim, self.dim))
            def mask_rejects(idx, i):
                if self.mask_weights is None:
                    return False
                return self.mask_weights[idx, i] == 0.
            for i in xrange(self.dim):
                assert self.sparse_init <= self.input_dim
                for j in xrange(self.sparse_init):
                    idx = rng.randint(0, self.input_dim)
                    while W[idx, i] != 0 or mask_rejects(idx, i):
                        idx = rng.randint(0, self.input_dim)
                    W[idx, i] = rng.randn()
            W *= self.sparse_stdev

        W = sharedX(W)
        W.name = self.layer_name + '_W'

        self.transformer = MatrixMul(W)

        W ,= self.transformer.get_params()
        assert W.name is not None

        if self.mask_weights is not None:
            expected_shape =  (self.input_dim, self.dim)
            if expected_shape != self.mask_weights.shape:
                raise ValueError("Expected mask with shape "+str(expected_shape)+" but got "+str(self.mask_weights.shape))
            self.mask = sharedX(self.mask_weights)

    def censor_updates(self, updates):

        switch = self.switch
        if switch in updates:
            updates[switch] = T.clip(updates[switch], 0., 1.)
        if self.mask_weights is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updates[W] = updates[W] * self.mask

        if self.max_row_norm is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                row_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=1))
                desired_norms = T.clip(row_norms, 0, self.max_row_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + row_norms)).dimshuffle(0, 'x')

        if self.max_col_norm is not None:
            assert self.max_row_norm is None
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                col_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=0))
                desired_norms = T.clip(col_norms, 0, self.max_col_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + col_norms))

    def get_params(self):
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        rval.append(self.switch)
        if self.use_bias:
            assert self.b.name is not None
            assert self.b not in rval
            rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def get_weights(self):
        if self.requires_reformat:
            # This is not really an unimplemented case.
            # We actually don't know how to format the weights
            # in design space. We got the data in topo space
            # and we don't have access to the dataset
            raise NotImplementedError()
        W ,= self.transformer.get_params()
        return W.get_value()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        assert self.use_bias
        self.b.set_value(biases)

    def get_biases(self):
        assert self.use_bias
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_topo(self):

        if not isinstance(self.input_space, Conv2DSpace):
            raise NotImplementedError()

        W ,= self.transformer.get_params()

        W = W.T

        W = W.reshape((self.dim, self.input_space.shape[0],
                       self.input_space.shape[1], self.input_space.num_channels))

        W = Conv2DSpace.convert(W, self.input_space.axes, ('b', 0, 1, 'c'))

        return function([], W)()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 2

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=1))
        col_norms = T.sqrt(sq_W.sum(axis=0))

        return OrderedDict([
                            ('row_norms_min'  , row_norms.min()),
                            ('row_norms_mean' , row_norms.mean()),
                            ('row_norms_max'  , row_norms.max()),
                            ('col_norms_min'  , col_norms.min()),
                            ('col_norms_mean' , col_norms.mean()),
                            ('col_norms_max'  , col_norms.max()),
                            ('switch_min', self.switch.min()),
                            ('switch_mean', self.switch.mean()),
                            ('switch_max', self.switch.max())
                            ])

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if sb.shape[0] != self.mlp.batch_size:
                        raise ValueError("self.dbm.batch_size is %d but got shape of %d" % (self.dbm.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below)
        if self.use_bias:
            z = z + self.b
        if self.layer_name is not None:
            z.name = self.layer_name + '_z'


        right = (self.switch * z + (1. - self.switch) * T.tanh(z)) * (z > 0.)
        left = z * (z <= .0)
        p = left + right

        if self.copy_input:
            p = T.concatenate((p, state_below), axis=1)

        return p

class MaxPoolRectifiedLinear(Layer):
    """
        A hidden layer that uses the softmax function to do
        max pooling over groups of units.
        When the pooling size is 1, this reduces to a standard
        sigmoidal MLP layer.
        """

    def __init__(self,
                 layer_name,
                 detector_layer_dim,
                 pool_size,
                 pool_stride = None,
                 randomize_pools = False,
                 irange = None,
                 sparse_init = None,
                 sparse_stdev = 1.,
                 include_prob = 1.0,
                 init_bias = 0.,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 max_col_norm = None,
                 max_row_norm = None,
                 mask_weights = None,
                 min_zero = False
        ):
        """

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

            """

        if pool_stride is None:
            pool_stride = pool_size

        self.__dict__.update(locals())
        del self.self

        self.b = sharedX( np.zeros((self.detector_layer_dim,)) + init_bias, name = layer_name + '_b')


        if max_row_norm is not None:
            raise NotImplementedError()

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        if isinstance(space, VectorSpace):
            self.requires_reformat = False
            self.input_dim = space.dim
        else:
            self.requires_reformat = True
            self.input_dim = space.get_total_dimension()
            self.desired_space = VectorSpace(self.input_dim)


        if not ((self.detector_layer_dim - self.pool_size) % self.pool_stride == 0):
            if self.pool_stride == self.pool_size:
                raise ValueError("detector_layer_dim = %d, pool_size = %d. Should be divisible but remainder is %d" %
                             (self.detector_layer_dim, self.pool_size, self.detector_layer_dim % self.pool_size))
            raise ValueError()

        self.h_space = VectorSpace(self.detector_layer_dim)
        self.pool_layer_dim = (self.detector_layer_dim - self.pool_size)/ self.pool_stride + 1
        self.output_space = VectorSpace(self.pool_layer_dim)

        rng = self.mlp.rng
        if self.irange is not None:
            assert self.sparse_init is None
            W = rng.uniform(-self.irange,
                            self.irange,
                            (self.input_dim, self.detector_layer_dim)) * \
                (rng.uniform(0.,1., (self.input_dim, self.detector_layer_dim))
                 < self.include_prob)
        else:
            assert self.sparse_init is not None
            W = np.zeros((self.input_dim, self.detector_layer_dim))
            def mask_rejects(idx, i):
                if self.mask_weights is None:
                    return False
                return self.mask_weights[idx, i] == 0.
            for i in xrange(self.detector_layer_dim):
                assert self.sparse_init <= self.input_dim
                for j in xrange(self.sparse_init):
                    idx = rng.randint(0, self.input_dim)
                    while W[idx, i] != 0 or mask_rejects(idx, i):
                        idx = rng.randint(0, self.input_dim)
                    W[idx, i] = rng.randn()
            W *= self.sparse_stdev

        W = sharedX(W)
        W.name = self.layer_name + '_W'

        self.transformer = MatrixMul(W)

        W ,= self.transformer.get_params()
        assert W.name is not None

        if not hasattr(self, 'randomize_pools'):
            self.randomize_pools = False

        if self.randomize_pools:
            permute = np.zeros((self.detector_layer_dim, self.detector_layer_dim))
            for j in xrange(self.detector_layer_dim):
                i = rng.randint(self.detector_layer_dim)
                permute[i,j] = 1
            self.permute = sharedX(permute)

        if self.mask_weights is not None:
            expected_shape =  (self.input_dim, self.detector_layer_dim)
            if expected_shape != self.mask_weights.shape:
                raise ValueError("Expected mask with shape "+str(expected_shape)+" but got "+str(self.mask_weights.shape))
            self.mask = sharedX(self.mask_weights)

    def censor_updates(self, updates):

        # Patch old pickle files
        if not hasattr(self, 'mask_weights'):
            self.mask_weights = None

        if self.mask_weights is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updates[W] = updates[W] * self.mask

        if self.max_col_norm is not None:
            assert self.max_row_norm is None
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                col_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=0))
                desired_norms = T.clip(col_norms, 0, self.max_col_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + col_norms))

    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def get_weights(self):
        if self.requires_reformat:
            # This is not really an unimplemented case.
            # We actually don't know how to format the weights
            # in design space. We got the data in topo space
            # and we don't have access to the dataset
            raise NotImplementedError()
        W ,= self.transformer.get_params()
        W = W.get_value()

        if not hasattr(self, 'randomize_pools'):
            self.randomize_pools = False

        if self.randomize_pools:
            warnings.warn("randomize_pools makes get_weights multiply by the permutation matrix. "
                    "If you call set_weights(W) and then call get_weights(), the return value will "
                    "WP not W.")
            P = self.permute.get_value()
            return np.dot(W,P)

        return W

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        self.b.set_value(biases)

    def get_biases(self):
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_view_shape(self):
        total = self.detector_layer_dim
        cols = self.pool_size
        if cols == 1:
            # Let the PatchViewer decide how to arrange the units
            # when they're not pooled
            raise NotImplementedError()
        # When they are pooled, make each pooling unit have one row
        rows = total // cols
        if rows * cols < total:
            rows = rows + 1
        return rows, cols


    def get_weights_topo(self):

        if not isinstance(self.input_space, Conv2DSpace):
            raise NotImplementedError()

        W ,= self.transformer.get_params()

        W = W.T

        W = W.reshape((self.detector_layer_dim, self.input_space.shape[0],
                       self.input_space.shape[1], self.input_space.num_channels))

        W = Conv2DSpace.convert(W, self.input_space.axes, ('b', 0, 1, 'c'))

        return function([], W)()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 2

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=1))
        col_norms = T.sqrt(sq_W.sum(axis=0))

        return OrderedDict([
                            ('row_norms_min'  , row_norms.min()),
                            ('row_norms_mean' , row_norms.mean()),
                            ('row_norms_max'  , row_norms.max()),
                            ('col_norms_min'  , col_norms.min()),
                            ('col_norms_mean' , col_norms.mean()),
                            ('col_norms_max'  , col_norms.max()),
                            ])


    def get_monitoring_channels_from_state(self, state):

        P = state

        rval = OrderedDict()

        if self.pool_size == 1:
            vars_and_prefixes = [ (P,'') ]
        else:
            vars_and_prefixes = [ (P, 'p_') ]

        for var, prefix in vars_and_prefixes:
            v_max = var.max(axis=0)
            v_min = var.min(axis=0)
            v_mean = var.mean(axis=0)
            v_range = v_max - v_min

            # max_x.mean_u is "the mean over *u*nits of the max over e*x*amples"
            # The x and u are included in the name because otherwise its hard
            # to remember which axis is which when reading the monitor
            # I use inner.outer rather than outer_of_inner or something like that
            # because I want mean_x.* to appear next to each other in the alphabetical
            # list, as these are commonly plotted together
            for key, val in [
                             ('max_x.max_u', v_max.max()),
                             ('max_x.mean_u', v_max.mean()),
                             ('max_x.min_u', v_max.min()),
                             ('min_x.max_u', v_min.max()),
                             ('min_x.mean_u', v_min.mean()),
                             ('min_x.min_u', v_min.min()),
                             ('range_x.max_u', v_range.max()),
                             ('range_x.mean_u', v_range.mean()),
                             ('range_x.min_u', v_range.min()),
                             ('mean_x.max_u', v_mean.max()),
                             ('mean_x.mean_u', v_mean.mean()),
                             ('mean_x.min_u', v_mean.min())
                             ]:
                rval[prefix+key] = val

        return rval

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if sb.shape[0] != self.dbm.batch_size:
                        raise ValueError("self.dbm.batch_size is %d but got shape of %d" % (self.dbm.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below) + self.b

        if not hasattr(self, 'randomize_pools'):
            self.randomize_pools = False

        if not hasattr(self, 'pool_stride'):
            self.pool_stride = self.pool_size

        if self.randomize_pools:
            z = T.dot(z, self.permute)

        if not hasattr(self, 'min_zero'):
            self.min_zero = False

        if self.min_zero:
            p = T.zeros_like(z)
        else:
            p = None

        last_start = self.detector_layer_dim  - self.pool_size
        for i in xrange(self.pool_size):
            cur = z[:,i:last_start+i+1:self.pool_stride]
            if p is None:
                p = cur
            else:
                p = T.maximum(cur, p)

        p.name = self.layer_name + '_p_'

        return p

    def foo(self, state_below):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if sb.shape[0] != self.dbm.batch_size:
                        raise ValueError("self.dbm.batch_size is %d but got shape of %d" % (self.dbm.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below) + self.b

        if not hasattr(self, 'randomize_pools'):
            self.randomize_pools = False

        if not hasattr(self, 'pool_stride'):
            self.pool_stride = self.pool_size

        if self.randomize_pools:
            z = T.dot(z, self.permute)

        if not hasattr(self, 'min_zero'):
            self.min_zero = False

        if self.min_zero:
            p = T.zeros_like(z)
        else:
            p = None

        last_start = self.detector_layer_dim  - self.pool_size

        pooling_stack = []
        for i in xrange(self.pool_size):
            cur = z[:,i:last_start+i+1:self.pool_stride]
            cur = cur.reshape((cur.shape[0], cur.shape[1], 1))
            assert cur.ndim == 3
            pooling_stack.append(cur)
        if self.min_zero:
            pooling_stack.append(T.zeros_like(cur))
        pooling_stack = T.concatenate(pooling_stack, axis=2)
        p = pooling_stack.max(axis=2)
        counts = (T.eq(pooling_stack, p.dimshuffle(0, 1, 'x'))).sum(axis=0)

        p.name = self.layer_name + '_p_'

        return p, counts

class TopoMaxPoolRectifiedLinear(Layer):
    """
        A hidden layer that uses the softmax function to do
        max pooling over groups of units.
        When the pooling size is 1, this reduces to a standard
        sigmoidal MLP layer.
        """

    def __init__(self,
                 layer_name,
                 detector_space,
                 pool_shape,
                 pool_stride,
                 irange = None,
                 sparse_init = None,
                 sparse_stdev = 1.,
                 include_prob = 1.0,
                 init_bias = 0.,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 max_col_norm = None,
                 max_row_norm = None,
                 mask_weights = None,
                 min_zero = False
        ):
        """
        note: detector_space.axes will be forced to ('b', 'c', 0, 1).

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

        """

        detector_space.axes = ('b', 'c', 0, 1)

        self.__dict__.update(locals())
        del self.self

        if max_row_norm is not None:
            raise NotImplementedError()

        assert isinstance(detector_space, Conv2DSpace)
        self.detector_layer_dim = detector_space.get_total_dimension()
        self.raw_detector_space = VectorSpace(self.detector_layer_dim)

        self.b = sharedX( np.zeros((self.detector_layer_dim,)) + init_bias, name = layer_name + '_b')

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        if isinstance(space, VectorSpace):
            self.requires_reformat = False
            self.input_dim = space.dim
        else:
            self.requires_reformat = True
            self.input_dim = space.get_total_dimension()
            self.desired_space = VectorSpace(self.input_dim)

        dummy_detector = sharedX(self.detector_space.get_origin_batch(1))
        print self.detector_space.axes
        dummy_h = max_pool(bc01 = dummy_detector, pool_shape=self.pool_shape, pool_stride=self.pool_stride,
                image_shape = self.detector_space.shape)
        dummy_h = dummy_h.eval()

        self.output_space = Conv2DSpace([dummy_h.shape[2], dummy_h.shape[3]], dummy_h.shape[1], axes=('b', 'c', 0, 1))

        rng = self.mlp.rng
        if self.irange is not None:
            assert self.sparse_init is None
            assert isinstance(self.input_dim, int)
            assert isinstance(self.detector_layer_dim, int)
            W = rng.uniform(-self.irange,
                            self.irange,
                            (self.input_dim, self.detector_layer_dim)) * \
                (rng.uniform(0.,1., (self.input_dim, self.detector_layer_dim))
                 < self.include_prob)
        else:
            assert self.sparse_init is not None
            W = np.zeros((self.input_dim, self.detector_layer_dim))
            def mask_rejects(idx, i):
                if self.mask_weights is None:
                    return False
                return self.mask_weights[idx, i] == 0.
            for i in xrange(self.detector_layer_dim):
                assert self.sparse_init <= self.input_dim
                for j in xrange(self.sparse_init):
                    idx = rng.randint(0, self.input_dim)
                    while W[idx, i] != 0 or mask_rejects(idx, i):
                        idx = rng.randint(0, self.input_dim)
                    W[idx, i] = rng.randn()
            W *= self.sparse_stdev

        W = sharedX(W)
        W.name = self.layer_name + '_W'

        self.transformer = MatrixMul(W)

        W ,= self.transformer.get_params()
        assert W.name is not None

        if self.mask_weights is not None:
            expected_shape =  (self.input_dim, self.detector_layer_dim)
            if expected_shape != self.mask_weights.shape:
                raise ValueError("Expected mask with shape "+str(expected_shape)+" but got "+str(self.mask_weights.shape))
            self.mask = sharedX(self.mask_weights)

    def censor_updates(self, updates):

        # Patch old pickle files
        if not hasattr(self, 'mask_weights'):
            self.mask_weights = None

        if self.mask_weights is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updates[W] = updates[W] * self.mask

        if self.max_col_norm is not None:
            assert self.max_row_norm is None
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                col_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=0))
                desired_norms = T.clip(col_norms, 0, self.max_col_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + col_norms))

    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def get_weights(self):
        warnings.warn(
                """!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                WRITE UNIT TEST THAT WEIGHTS VIEW IS LAID OUT RIGHT
                !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                """)
        if self.requires_reformat:
            # This is not really an unimplemented case.
            # We actually don't know how to format the weights
            # in design space. We got the data in topo space
            # and we don't have access to the dataset
            raise NotImplementedError()
        W ,= self.transformer.get_params()
        return W.get_value()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        self.b.set_value(biases)

    def get_biases(self):
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_view_shape(self):
        total = self.detector_layer_dim
        cols = self.detector_space.shape[1]
        if cols == 1:
            # Let the PatchViewer decide how to arrange the units
            # when they're not pooled
            raise NotImplementedError()
        # When they are pooled, make each pooling unit have one row
        rows = total // cols
        if rows * cols < total:
            rows = rows + 1
        return rows, cols


    def get_weights_topo(self):

        if not isinstance(self.input_space, Conv2DSpace):
            raise NotImplementedError()

        W ,= self.transformer.get_params()

        W = W.T

        W = W.reshape((self.detector_layer_dim, self.input_space.shape[0],
                       self.input_space.shape[1], self.input_space.num_channels))

        W = Conv2DSpace.convert(W, self.input_space.axes, ('b', 0, 1, 'c'))

        return function([], W)()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 2

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=1))
        col_norms = T.sqrt(sq_W.sum(axis=0))

        return OrderedDict([
                            ('row_norms_min'  , row_norms.min()),
                            ('row_norms_mean' , row_norms.mean()),
                            ('row_norms_max'  , row_norms.max()),
                            ('col_norms_min'  , col_norms.min()),
                            ('col_norms_mean' , col_norms.mean()),
                            ('col_norms_max'  , col_norms.max()),
                            ])


    def get_monitoring_channels_from_state(self, state):

        P = state

        rval = OrderedDict()

        vars_and_prefixes = [ (P, 'p_') ]

        for var, prefix in vars_and_prefixes:
            v_max = var.max(axis=0)
            v_min = var.min(axis=0)
            v_mean = var.mean(axis=0)
            v_range = v_max - v_min

            # max_x.mean_u is "the mean over *u*nits of the max over e*x*amples"
            # The x and u are included in the name because otherwise its hard
            # to remember which axis is which when reading the monitor
            # I use inner.outer rather than outer_of_inner or something like that
            # because I want mean_x.* to appear next to each other in the alphabetical
            # list, as these are commonly plotted together
            for key, val in [
                             ('max_x.max_u', v_max.max()),
                             ('max_x.mean_u', v_max.mean()),
                             ('max_x.min_u', v_max.min()),
                             ('min_x.max_u', v_min.max()),
                             ('min_x.mean_u', v_min.mean()),
                             ('min_x.min_u', v_min.min()),
                             ('range_x.max_u', v_range.max()),
                             ('range_x.mean_u', v_range.mean()),
                             ('range_x.min_u', v_range.min()),
                             ('mean_x.max_u', v_mean.max()),
                             ('mean_x.mean_u', v_mean.mean()),
                             ('mean_x.min_u', v_mean.min())
                             ]:
                rval[prefix+key] = val

        return rval

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if self.mlp.batch_size is not None and sb.shape[0] != self.mlp.batch_size:
                        raise ValueError("self.mlp.batch_size is %d but got shape of %d" % (self.mlp.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below) + self.b

        if not hasattr(self, 'min_zero'):
            self.min_zero = False

        z = self.raw_detector_space.format_as(z, self.detector_space)

        p = max_pool(bc01 = z, pool_shape=self.pool_shape, pool_stride=self.pool_stride,
                image_shape = self.detector_space.shape)

        if self.min_zero:
            p = T.maximum(0., p)

        p.name = self.layer_name + '_p_'

        return p

class TwoMaxPoolRectifiedLinear(Layer):
    """
        A hidden layer that uses the softmax function to do
        max pooling over groups of units.
        When the pooling size is 1, this reduces to a standard
        sigmoidal MLP layer.
        """

    def __init__(self,
                 layer_name,
                 detector_space,
                 irange = None,
                 sparse_init = None,
                 sparse_stdev = 1.,
                 include_prob = 1.0,
                 init_bias = 0.,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 max_col_norm = None,
                 max_row_norm = None,
                 mask_weights = None,
                 min_zero = False
        ):
        """
        note: detector_space.axes will be forced to ('b', 'c', 0, 1).

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

        """

        detector_space.axes = ('b', 'c', 0, 1)

        self.__dict__.update(locals())
        del self.self

        if max_row_norm is not None:
            raise NotImplementedError()

        assert isinstance(detector_space, Conv2DSpace)
        self.detector_layer_dim = detector_space.get_total_dimension()
        self.raw_detector_space = VectorSpace(self.detector_layer_dim)

        self.b = sharedX( np.zeros((self.detector_layer_dim,)) + init_bias, name = layer_name + '_b')

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        if isinstance(space, VectorSpace):
            self.requires_reformat = False
            self.input_dim = space.dim
        else:
            self.requires_reformat = True
            self.input_dim = space.get_total_dimension()
            self.desired_space = VectorSpace(self.input_dim)

        self.output_space = VectorSpace(self.detector_space.num_channels *
                (self.detector_space.shape[0] +
                self.detector_space.shape[1]))

        rng = self.mlp.rng
        if self.irange is not None:
            assert self.sparse_init is None
            assert isinstance(self.input_dim, int)
            assert isinstance(self.detector_layer_dim, int)
            W = rng.uniform(-self.irange,
                            self.irange,
                            (self.input_dim, self.detector_layer_dim)) * \
                (rng.uniform(0.,1., (self.input_dim, self.detector_layer_dim))
                 < self.include_prob)
        else:
            assert self.sparse_init is not None
            W = np.zeros((self.input_dim, self.detector_layer_dim))
            def mask_rejects(idx, i):
                if self.mask_weights is None:
                    return False
                return self.mask_weights[idx, i] == 0.
            for i in xrange(self.detector_layer_dim):
                assert self.sparse_init <= self.input_dim
                for j in xrange(self.sparse_init):
                    idx = rng.randint(0, self.input_dim)
                    while W[idx, i] != 0 or mask_rejects(idx, i):
                        idx = rng.randint(0, self.input_dim)
                    W[idx, i] = rng.randn()
            W *= self.sparse_stdev

        W = sharedX(W)
        W.name = self.layer_name + '_W'

        self.transformer = MatrixMul(W)

        W ,= self.transformer.get_params()
        assert W.name is not None

        if self.mask_weights is not None:
            expected_shape =  (self.input_dim, self.detector_layer_dim)
            if expected_shape != self.mask_weights.shape:
                raise ValueError("Expected mask with shape "+str(expected_shape)+" but got "+str(self.mask_weights.shape))
            self.mask = sharedX(self.mask_weights)

    def censor_updates(self, updates):

        # Patch old pickle files
        if not hasattr(self, 'mask_weights'):
            self.mask_weights = None

        if self.mask_weights is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updates[W] = updates[W] * self.mask

        if self.max_col_norm is not None:
            assert self.max_row_norm is None
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                col_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=0))
                desired_norms = T.clip(col_norms, 0, self.max_col_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + col_norms))

    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def get_weights(self):
        warnings.warn(
                """!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                WRITE UNIT TEST THAT WEIGHTS VIEW IS LAID OUT RIGHT
                !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                """)
        if self.requires_reformat:
            # This is not really an unimplemented case.
            # We actually don't know how to format the weights
            # in design space. We got the data in topo space
            # and we don't have access to the dataset
            raise NotImplementedError()
        W ,= self.transformer.get_params()
        return W.get_value()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        self.b.set_value(biases)

    def get_biases(self):
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_view_shape(self):
        total = self.detector_layer_dim
        cols = self.detector_space.shape[1]
        if cols == 1:
            # Let the PatchViewer decide how to arrange the units
            # when they're not pooled
            raise NotImplementedError()
        # When they are pooled, make each pooling unit have one row
        rows = total // cols
        if rows * cols < total:
            rows = rows + 1
        return rows, cols


    def get_weights_topo(self):

        if not isinstance(self.input_space, Conv2DSpace):
            raise NotImplementedError()

        W ,= self.transformer.get_params()

        W = W.T

        W = W.reshape((self.detector_layer_dim, self.input_space.shape[0],
                       self.input_space.shape[1], self.input_space.num_channels))

        W = Conv2DSpace.convert(W, self.input_space.axes, ('b', 0, 1, 'c'))

        return function([], W)()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 2

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=1))
        col_norms = T.sqrt(sq_W.sum(axis=0))

        return OrderedDict([
                            ('row_norms_min'  , row_norms.min()),
                            ('row_norms_mean' , row_norms.mean()),
                            ('row_norms_max'  , row_norms.max()),
                            ('col_norms_min'  , col_norms.min()),
                            ('col_norms_mean' , col_norms.mean()),
                            ('col_norms_max'  , col_norms.max()),
                            ])


    def get_monitoring_channels_from_state(self, state):

        P = state

        rval = OrderedDict()

        vars_and_prefixes = [ (P, 'p_') ]

        for var, prefix in vars_and_prefixes:
            v_max = var.max(axis=0)
            v_min = var.min(axis=0)
            v_mean = var.mean(axis=0)
            v_range = v_max - v_min

            # max_x.mean_u is "the mean over *u*nits of the max over e*x*amples"
            # The x and u are included in the name because otherwise its hard
            # to remember which axis is which when reading the monitor
            # I use inner.outer rather than outer_of_inner or something like that
            # because I want mean_x.* to appear next to each other in the alphabetical
            # list, as these are commonly plotted together
            for key, val in [
                             ('max_x.max_u', v_max.max()),
                             ('max_x.mean_u', v_max.mean()),
                             ('max_x.min_u', v_max.min()),
                             ('min_x.max_u', v_min.max()),
                             ('min_x.mean_u', v_min.mean()),
                             ('min_x.min_u', v_min.min()),
                             ('range_x.max_u', v_range.max()),
                             ('range_x.mean_u', v_range.mean()),
                             ('range_x.min_u', v_range.min()),
                             ('mean_x.max_u', v_mean.max()),
                             ('mean_x.mean_u', v_mean.mean()),
                             ('mean_x.min_u', v_mean.min())
                             ]:
                rval[prefix+key] = val

        return rval

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if self.mlp.batch_size is not None and sb.shape[0] != self.mlp.batch_size:
                        raise ValueError("self.mlp.batch_size is %d but got shape of %d" % (self.mlp.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below) + self.b

        if not hasattr(self, 'min_zero'):
            self.min_zero = False

        z = self.raw_detector_space.format_as(z, self.detector_space)
        assert z.ndim == 4

        max1 = z.max(axis=3)
        assert max1.ndim == 3
        max1 = max1.reshape((max1.shape[0], max1.shape[1] * max1.shape[2]))
        max2 = z.max(axis=2)
        max2 = max2.reshape((max2.shape[0], max2.shape[1] * max2.shape[2]))

        p = T.concatenate((max1, max2), axis=1)

        if self.min_zero:
            p = T.maximum(0., p)

        p.name = self.layer_name + '_p_'

        return p

class ConvLinear(Layer):
    """
        Linear filters. Pools across channels in
        non-overlapping groups of channel_pool_size,
        then pools spatially.
    """

    def __init__(self,
                 detector_channels,
                 kernel_shape,
                 pool_shape,
                 pool_stride,
                 layer_name,
                 channel_pool_size = 1,
                 irange = None,
                 border_mode = 'valid',
                 sparse_init = None,
                 include_prob = 1.0,
                 init_bias = 0.,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 max_kernel_norm = None):
        """

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

        """
        self.__dict__.update(locals())
        del self.self

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space
        rng = self.mlp.rng

        if self.border_mode == 'valid':
            output_shape = [self.input_space.shape[0] - self.kernel_shape[0] + 1,
                self.input_space.shape[1] - self.kernel_shape[1] + 1]
        elif self.border_mode == 'full':
            output_shape = [self.input_space.shape[0] + self.kernel_shape[0] - 1,
                    self.input_space.shape[1] + self.kernel_shape[1] - 1]

        self.detector_space = Conv2DSpace(shape=output_shape,
                num_channels = self.detector_channels,
                axes = ('b', 'c', 0, 1))

        if self.irange is not None:
            assert self.sparse_init is None
            self.transformer = conv2d.make_random_conv2D(
                    irange = self.irange,
                    input_space = self.input_space,
                    output_space = self.detector_space,
                    kernel_shape = self.kernel_shape,
                    batch_size = self.mlp.batch_size,
                    subsample = (1,1),
                    border_mode = self.border_mode,
                    rng = rng)
        elif self.sparse_init is not None:
            self.transformer = conv2d.make_sparse_random_conv2D(
                    num_nonzero = self.sparse_init,
                    input_space = self.input_space,
                    output_space = self.detector_space,
                    kernel_shape = self.kernel_shape,
                    batch_size = self.mlp.batch_size,
                    subsample = (1,1),
                    border_mode = self.border_mode,
                    rng = rng)
        W, = self.transformer.get_params()
        W.name = 'W'

        self.b = sharedX(self.detector_space.get_origin() + self.init_bias)
        self.b.name = 'b'

        print 'Input shape: ', self.input_space.shape
        print 'Detector space: ', self.detector_space.shape

        if self.mlp.batch_size is None:
            raise ValueError("Tried to use a convolutional layer with an MLP that has "
                    "no batch size specified. You must specify the batch size of the "
                    "model because theano requires the batch size to be known at "
                    "graph construction time for convolution.")

        dummy_detector = sharedX(self.detector_space.get_origin_batch(self.mlp.batch_size))

        dummy_p = max_pool(bc01=dummy_detector, pool_shape=self.pool_shape,
                pool_stride=self.pool_stride,
                image_shape=self.detector_space.shape)
        dummy_p = dummy_p.eval()
        assert self.detector_channels % self.channel_pool_size == 0
        self.output_space = Conv2DSpace(shape=[dummy_p.shape[2], dummy_p.shape[3]],
                num_channels = self.detector_channels // self.channel_pool_size, axes = ('b', 'c', 0, 1) )

        print 'Output space: ', self.output_space.shape



    def censor_updates(self, updates):

        if self.max_kernel_norm is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                row_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=(1,2,3)))
                desired_norms = T.clip(row_norms, 0, self.max_kernel_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + row_norms)).dimshuffle(0, 'x', 'x', 'x')


    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        self.b.set_value(biases)

    def get_biases(self):
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_topo(self):
        outp, inp, rows, cols = range(4)
        raw = self.transformer._filters.get_value()

        return np.transpose(raw, (outp,rows,cols,inp))

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 4

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=(1,2,3)))

        return OrderedDict([
                            ('kernel_norms_min'  , row_norms.min()),
                            ('kernel_norms_mean' , row_norms.mean()),
                            ('kernel_norms_max'  , row_norms.max()),
                            ])

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        z = self.transformer.lmul(state_below) + self.b
        if self.layer_name is not None:
            z.name = self.layer_name + '_z'

        self.detector_space.validate(z)

        if self.channel_pool_size != 1:
            s = None
            for i in xrange(self.channel_pool_size):
                t = z[:,i::self.channel_pool_size,:,:]
                if s is None:
                    s = t
                else:
                    s = T.maximum(s, t)
            z = s

        p = max_pool(bc01=z, pool_shape=self.pool_shape,
                pool_stride=self.pool_stride,
                image_shape=self.detector_space.shape)

        self.output_space.validate(p)

        return p

    def get_weights_view_shape(self):
        total = self.detector_channels
        cols = self.channel_pool_size
        if cols == 1:
            # Let the PatchViewer decide how to arrange the units
            # when they're not pooled
            raise NotImplementedError()
        # When they are pooled, make each pooling unit have one row
        rows = total // cols
        if rows * cols < total:
            rows = rows + 1
        return rows, cols

class ConvLinearC01B(Layer):
    """
    Like ConvLinear but for (c, 0, 1, b) axes.
    """

    def __init__(self,
                 detector_channels,
                 kernel_shape,
                 pool_shape,
                 pool_stride,
                 layer_name,
                 channel_pool_size = 1,
                 irange = None,
                 sparse_init = None,
                 include_prob = 1.0,
                 init_bias = 0.,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 pad = 0,
                 fix_pool_shape = False,
                 fix_pool_stride = False,
                 fix_kernel_shape = False,
                 partial_sum = 1,
                 tied_b = False,
                 max_kernel_norm = None,
                 input_normalization = None,
                 output_normalization = None):
        """

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

            fix_pool_shape: If True, will modify self.pool_shape to avoid having
                            pool shape bigger than the entire detector layer.
                            If you have this on, you should probably also have
                            fix_pool_stride on, since the pool shape might shrink
                            smaller than the stride, even if the stride was initially
                            valid.
            fix_kernel_shape: if True, will modify self.kernel_shape to avoid
                            having the kernel shape bigger than the implicitly
                            zero padded input layer
            partial_sum: a parameter that influences the performance
        """

        self.__dict__.update(locals())
        del self.self

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        assert isinstance(self.input_space, Conv2DSpace)
        # note: I think the desired space thing is actually redundant,
        # since LinearTransform will also dimshuffle the axes if needed
        # It's not hurting anything to have it here but we could reduce
        # code complexity by removing it
        self.desired_space = Conv2DSpace(shape=space.shape,
                channels=space.num_channels,
                axes=('c', 0, 1, 'b'))

        ch = self.desired_space.num_channels
        rem = ch % 4
        if ch > 3 and rem != 0:
            self.dummy_channels = 4 - rem
        else:
            self.dummy_channels = 0
        self.dummy_space = Conv2DSpace(shape=space.shape,
                channels=space.num_channels + self.dummy_channels,
                axes=('c', 0, 1, 'b'))

        rng = self.mlp.rng

        output_shape = [self.input_space.shape[0] + 2 * self.pad - self.kernel_shape[0] + 1,
                self.input_space.shape[1] + 2 * self.pad - self.kernel_shape[1] + 1]

        def handle_kernel_shape(idx):
            if self.kernel_shape[idx] < 1:
                raise ValueError("kernel must have strictly positive size on all axes but has shape: "+str(self.kernel_shape))
            if output_shape[idx] <= 0:
                if self.fix_kernel_shape:
                    self.kernel_shape[idx] = self.input_space.shape[idx] + 2 * self.pad
                    assert self.kernel_shape[idx] != 0
                    output_shape[idx] = 1
                    warnings.warn("Had to change the kernel shape to make network feasible")
                else:
                    raise ValueError("kernel too big for input (even with zero padding)")

        map(handle_kernel_shape, [0, 1])

        self.detector_space = Conv2DSpace(shape=output_shape,
                num_channels = self.detector_channels,
                axes = ('c', 0, 1, 'b'))

        def handle_pool_shape(idx):
            if self.pool_shape[idx] < 1:
                raise ValueError("bad pool shape: " + str(self.pool_shape))
            if self.pool_shape[idx] > output_shape[idx]:
                if self.fix_pool_shape:
                    assert output_shape[idx] > 0
                    self.pool_shape[idx] = output_shape[idx]
                else:
                    raise ValueError("Pool shape exceeds detector layer shape on axis %d" % idx)

        map(handle_pool_shape, [0, 1])

        assert self.pool_shape[0] == self.pool_shape[1]
        assert self.pool_stride[0] == self.pool_stride[1]
        assert all(isinstance(elem, int) for elem in self.pool_stride)
        if self.pool_stride[0] > self.pool_shape[0]:
            if self.fix_pool_stride:
                warnings.warn("Fixing the pool stride")
                ps = self.pool_shape[0]
                assert isinstance(ps, int)
                self.pool_stride = [ps, ps]
            else:
                raise ValueError("Stride too big.")
        assert all(isinstance(elem, int) for elem in self.pool_stride)

        if self.irange is not None:
            assert self.sparse_init is None
            self.transformer = conv2d_c01b.make_random_conv2D(
                    irange = self.irange,
                    input_axes = self.desired_space.axes,
                    output_axes = self.detector_space.axes,
                    input_channels = self.dummy_space.num_channels,
                    output_channels = self.detector_space.num_channels,
                    kernel_shape = self.kernel_shape,
                    subsample = (1,1),
                    pad = self.pad,
                    partial_sum = self.partial_sum,
                    rng = rng)
        elif self.sparse_init is not None:
            self.transformer = conv2d_c01b.make_sparse_random_conv2D(
                    num_nonzero = self.sparse_init,
                    input_space = self.dummy_space,
                    output_space = self.detector_space,
                    kernel_shape = self.kernel_shape,
                    batch_size = self.mlp.batch_size,
                    subsample = (1,1),
                    border_mode = self.border_mode,
                    rng = rng)
        W, = self.transformer.get_params()
        W.name = 'W'

        if self.tied_b:
            self.b = sharedX(np.zeros((self.detector_space.num_channels)) + self.init_bias)
        else:
            self.b = sharedX(self.detector_space.get_origin() + self.init_bias)
        self.b.name = 'b'

        print 'Input shape: ', self.input_space.shape
        print 'Detector space: ', self.detector_space.shape

        assert self.detector_space.num_channels >= 16

        dummy_detector = sharedX(self.detector_space.get_origin_batch(2)[0:16,:,:,:])

        dummy_p = max_pool_c01b(c01b=dummy_detector, pool_shape=self.pool_shape,
                pool_stride=self.pool_stride,
                image_shape=self.detector_space.shape)
        dummy_p = dummy_p.eval()
        assert self.detector_channels % self.channel_pool_size == 0
        self.output_space = Conv2DSpace(shape=[dummy_p.shape[1], dummy_p.shape[2]],
                num_channels = self.detector_channels // self.channel_pool_size, axes = ('c', 0, 1, 'b') )

        print 'Output space: ', self.output_space.shape

    def censor_updates(self, updates):

        if self.max_kernel_norm is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                row_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=(0,1,2)))
                desired_norms = T.clip(row_norms, 0, self.max_kernel_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + row_norms)).dimshuffle('x', 'x', 'x', 0)

    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        self.b.set_value(biases)

    def get_biases(self):
        return self.b.get_value()

    def get_weights_topo(self):
        return self.transformer.get_weights_topo()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 4

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=(0,1,2)))

        return OrderedDict([
                            ('kernel_norms_min'  , row_norms.min()),
                            ('kernel_norms_mean' , row_norms.mean()),
                            ('kernel_norms_max'  , row_norms.max()),
                            ])

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        state_below = self.input_space.format_as(state_below, self.desired_space)

        if not hasattr(self, 'input_normalization'):
            self.input_normalization = None

        if self.input_normalization:
            state_below = self.input_normalization(state_below)

        # Alex's code requires # input channels to be <= 3 or a multiple of 4
        # so we add dummy channels if necessary
        if not hasattr(self, 'dummy_channels'):
            self.dummy_channels = 0
        if self.dummy_channels > 0:
            state_below = T.concatenate((state_below,
                T.zeros_like(state_below[0:self.dummy_channels, :, :, :])),
                axis=0)

        z = self.transformer.lmul(state_below)
        if not hasattr(self, 'tied_b'):
            self.tied_b = False
        if self.tied_b:
            b = self.b.dimshuffle(0, 'x', 'x', 'x')
        else:
            b = self.b.dimshuffle(0, 1, 2, 'x')


        z = z + b
        if self.layer_name is not None:
            z.name = self.layer_name + '_z'

        self.detector_space.validate(z)

        assert self.detector_space.num_channels % 16 == 0

        if self.output_space.num_channels % 16 == 0:
            # alex's max pool op only works when the number of channels
            # is divisible by 16. we can only do the cross-channel pooling
            # first if the cross-channel pooling preserves that property
            if self.channel_pool_size != 1:
                s = None
                for i in xrange(self.channel_pool_size):
                    t = z[i::self.channel_pool_size,:,:,:]
                    if s is None:
                        s = t
                    else:
                        s = T.maximum(s, t)
                z = s

            p = max_pool_c01b(c01b=z, pool_shape=self.pool_shape,
                    pool_stride=self.pool_stride,
                    image_shape=self.detector_space.shape)
        else:
            z = max_pool_c01b(c01b=z, pool_shape=self.pool_shape,
                    pool_stride=self.pool_stride,
                    image_shape=self.detector_space.shape)
            if self.channel_pool_size != 1:
                s = None
                for i in xrange(self.channel_pool_size):
                    t = z[i::self.channel_pool_size,:,:,:]
                    if s is None:
                        s = t
                    else:
                        s = T.maximum(s, t)
                z = s
            p = z


        self.output_space.validate(p)

        if not hasattr(self, 'output_normalization'):
            self.output_normalization = None

        if self.output_normalization:
            p = self.output_normalization(p)

        return p

    def get_weights_view_shape(self):
        total = self.detector_channels
        cols = self.channel_pool_size
        if cols == 1:
            # Let the PatchViewer decide how to arrange the units
            # when they're not pooled
            raise NotImplementedError()
        # When they are pooled, make each pooling unit have one row
        rows = total // cols
        if rows * cols < total:
            rows = rows + 1
        return rows, cols

    def get_monitoring_channels_from_state(self, state):

        P = state

        rval = OrderedDict()

        vars_and_prefixes = [ (P,'') ]

        for var, prefix in vars_and_prefixes:
            assert var.ndim == 4
            v_max = var.max(axis=(1,2,3))
            v_min = var.min(axis=(1,2,3))
            v_mean = var.mean(axis=(1,2,3))
            v_range = v_max - v_min

            # max_x.mean_u is "the mean over *u*nits of the max over e*x*amples"
            # The x and u are included in the name because otherwise its hard
            # to remember which axis is which when reading the monitor
            # I use inner.outer rather than outer_of_inner or something like that
            # because I want mean_x.* to appear next to each other in the alphabetical
            # list, as these are commonly plotted together
            for key, val in [
                             ('max_x.max_u', v_max.max()),
                             ('max_x.mean_u', v_max.mean()),
                             ('max_x.min_u', v_max.min()),
                             ('min_x.max_u', v_min.max()),
                             ('min_x.mean_u', v_min.mean()),
                             ('min_x.min_u', v_min.min()),
                             ('range_x.max_u', v_range.max()),
                             ('range_x.mean_u', v_range.mean()),
                             ('range_x.min_u', v_range.min()),
                             ('mean_x.max_u', v_mean.max()),
                             ('mean_x.mean_u', v_mean.mean()),
                             ('mean_x.min_u', v_mean.min())
                             ]:
                rval[prefix+key] = val

        return rval

# make old imports work
try:
    from pylearn2.monitor import get_channel
    from pylearn2.expr.normalize import CrossChannelNormalization
    from pylearn2.expr.normalize import CudaConvNetCrossChannelNormalization
except ImportError:
    warnings.warn("Some imports didn't work")

from pylearn2.models.mlp import MLP
class ChannelSync(MLP):
    """
    An MLP that drops out entire channels at once when applying
    dropout to 4-tensors. Assumes C01B tensor format.
    """

    def apply_dropout(self, state, include_prob, scale, theano_rng):
        if include_prob in [None, 1.0, 1]:
            return state
        assert scale is not None
        if isinstance(state, tuple):
            return tuple(self.apply_dropout(substate, include_prob, scale, theano_rng) for substate in state)
        if state.ndim == 4:
            mask = theano_rng.binomial(p=include_prob, size=(state.shape[0], state.shape[3]), dtype=state.dtype)
            mask = mask.dimshuffle(0, 'x', 'x', 1) * scale
            return state * mask
        return state * theano_rng.binomial(p=include_prob, size=state.shape, dtype=state.dtype) * scale
from pylearn2.models.mlp import MLP

class TwoStage(MLP):
    """
    An MLP that drops out entire channels at once when applying
    dropout to 4-tensors. Assumes C01B tensor format.
    Then drops individual units within the channel.
    Uses sqrt(include_prob) at both stages so the final probability
    of being included is still include_prob
    """

    def apply_dropout(self, state, include_prob, scale, theano_rng):
        if include_prob in [None, 1.0, 1]:
            return state
        assert scale is not None
        if isinstance(state, tuple):
            return tuple(self.apply_dropout(substate, include_prob, scale, theano_rng) for substate in state)
        if state.ndim == 4:
            mask = theano_rng.binomial(p=np.sqrt(include_prob), size=(state.shape[0], state.shape[3]), dtype=state.dtype)
            mask = mask.dimshuffle(0, 'x', 'x', 1)
            mask2 = theano_rng.binomial(p=np.sqrt(include_prob), size=state.shape, dtype=state.dtype)
            return state * mask * mask2 * scale
        return state * theano_rng.binomial(p=include_prob, size=state.shape, dtype=state.dtype) * scale

def ave_pool_c01b(c01b, pool_shape, pool_stride, image_shape):
    """
    """
    ave = None
    r, c = image_shape
    pr, pc = pool_shape
    rs, cs = pool_stride
    assert pr > 0
    assert pc > 0
    assert pr <= r
    assert pc <= c

    # Compute index in pooled space of last needed pool
    # (needed = each input pixel must appear in at least one pool)
    def last_pool(im_shp, p_shp, p_strd):
        rval = int(np.ceil(float(im_shp - p_shp) / p_strd))
        assert p_strd * rval + p_shp >= im_shp
        assert p_strd * (rval - 1) + p_shp < im_shp
        return rval
    # Compute starting row of the last pool
    last_pool_r = last_pool(image_shape[0] ,pool_shape[0], pool_stride[0]) * pool_stride[0]
    # Compute number of rows needed in image for all indexes to work out
    required_r = last_pool_r + pr

    last_pool_c = last_pool(image_shape[1] ,pool_shape[1], pool_stride[1]) * pool_stride[1]
    required_c = last_pool_c + pc

    for c01bv in get_debug_values(c01b):
        assert not np.any(np.isinf(c01bv))
        assert c01bv.shape[1] == r
        assert c01bv.shape[2] == c

    wide_zero = T.alloc(0., c01b.shape[0], required_r, required_c, c01b.shape[3])


    name = c01b.name
    if name is None:
        name = 'anon_bc01'
    c01b = T.set_subtensor(wide_zero[:, 0:r, 0:c, :], c01b)
    c01b.name = 'zero_padded_' + name

    for row_within_pool in xrange(pool_shape[0]):
        row_stop = last_pool_r + row_within_pool + 1
        for col_within_pool in xrange(pool_shape[1]):
            col_stop = last_pool_c + col_within_pool + 1
            cur = c01b[:,row_within_pool:row_stop:rs, col_within_pool:col_stop:cs, :]
            cur.name = 'max_pool_cur_'+c01b.name+'_'+str(row_within_pool)+'_'+str(col_within_pool)
            if ave is None:
                ave = cur
            else:
                ave = ave + cur
                ave.name = 'ave_pool_ave_'+c01b.name+'_'+str(row_within_pool)+'_'+str(col_within_pool)

    ave /= (pool_shape[0] * pool_shape[1])
    warnings.warn("Divides boundary pools by full pool size")

    ave.name = 'ave_pool('+name+')'

    return ave

class ConvLinearAveC01B(Layer):
    """
    Like ConvLinear but for (c, 0, 1, b) axes.
    """

    def __init__(self,
                 detector_channels,
                 kernel_shape,
                 pool_shape,
                 pool_stride,
                 layer_name,
                 channel_pool_size = 1,
                 irange = None,
                 sparse_init = None,
                 include_prob = 1.0,
                 init_bias = 0.,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 pad = 0,
                 fix_pool_shape = False,
                 fix_pool_stride = False,
                 fix_kernel_shape = False,
                 partial_sum = 1,
                 max_kernel_norm = None,
                 input_normalization = None,
                 output_normalization = None):
        """

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

            fix_pool_shape: If True, will modify self.pool_shape to avoid having
                            pool shape bigger than the entire detector layer.
                            If you have this on, you should probably also have
                            fix_pool_stride on, since the pool shape might shrink
                            smaller than the stride, even if the stride was initially
                            valid.
            fix_kernel_shape: if True, will modify self.kernel_shape to avoid
                            having the kernel shape bigger than the implicitly
                            zero padded input layer
            partial_sum: a parameter that influences the performance
        """

        self.__dict__.update(locals())
        del self.self

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        assert isinstance(self.input_space, Conv2DSpace)
        # note: I think the desired space thing is actually redundant,
        # since LinearTransform will also dimshuffle the axes if needed
        # It's not hurting anything to have it here but we could reduce
        # code complexity by removing it
        self.desired_space = Conv2DSpace(shape=space.shape,
                channels=space.num_channels,
                axes=('c', 0, 1, 'b'))

        ch = self.desired_space.num_channels
        rem = ch % 4
        if ch > 3 and rem != 0:
            self.dummy_channels = 4 - rem
        else:
            self.dummy_channels = 0
        self.dummy_space = Conv2DSpace(shape=space.shape,
                channels=space.num_channels + self.dummy_channels,
                axes=('c', 0, 1, 'b'))

        rng = self.mlp.rng

        output_shape = [self.input_space.shape[0] + 2 * self.pad - self.kernel_shape[0] + 1,
                self.input_space.shape[1] + 2 * self.pad - self.kernel_shape[1] + 1]

        def handle_kernel_shape(idx):
            if self.kernel_shape[idx] < 1:
                raise ValueError("kernel must have strictly positive size on all axes but has shape: "+str(self.kernel_shape))
            if output_shape[idx] <= 0:
                if self.fix_kernel_shape:
                    self.kernel_shape[idx] = self.input_space.shape[idx] + 2 * self.pad
                    assert self.kernel_shape[idx] != 0
                    output_shape[idx] = 1
                    warnings.warn("Had to change the kernel shape to make network feasible")
                else:
                    raise ValueError("kernel too big for input (even with zero padding)")

        map(handle_kernel_shape, [0, 1])

        self.detector_space = Conv2DSpace(shape=output_shape,
                num_channels = self.detector_channels,
                axes = ('c', 0, 1, 'b'))

        def handle_pool_shape(idx):
            if self.pool_shape[idx] < 1:
                raise ValueError("bad pool shape: " + str(self.pool_shape))
            if self.pool_shape[idx] > output_shape[idx]:
                if self.fix_pool_shape:
                    assert output_shape[idx] > 0
                    self.pool_shape[idx] = output_shape[idx]
                else:
                    raise ValueError("Pool shape exceeds detector layer shape on axis %d" % idx)

        map(handle_pool_shape, [0, 1])

        assert self.pool_shape[0] == self.pool_shape[1]
        assert self.pool_stride[0] == self.pool_stride[1]
        assert all(isinstance(elem, int) for elem in self.pool_stride)
        if self.pool_stride[0] > self.pool_shape[0]:
            if self.fix_pool_stride:
                warnings.warn("Fixing the pool stride")
                ps = self.pool_shape[0]
                assert isinstance(ps, int)
                self.pool_stride = [ps, ps]
            else:
                raise ValueError("Stride too big.")
        assert all(isinstance(elem, int) for elem in self.pool_stride)

        if self.irange is not None:
            assert self.sparse_init is None
            self.transformer = conv2d_c01b.make_random_conv2D(
                    irange = self.irange,
                    input_axes = self.desired_space.axes,
                    output_axes = self.detector_space.axes,
                    input_channels = self.dummy_space.num_channels,
                    output_channels = self.detector_space.num_channels,
                    kernel_shape = self.kernel_shape,
                    subsample = (1,1),
                    pad = self.pad,
                    partial_sum = self.partial_sum,
                    rng = rng)
        elif self.sparse_init is not None:
            self.transformer = conv2d_c01b.make_sparse_random_conv2D(
                    num_nonzero = self.sparse_init,
                    input_space = self.dummy_space,
                    output_space = self.detector_space,
                    kernel_shape = self.kernel_shape,
                    batch_size = self.mlp.batch_size,
                    subsample = (1,1),
                    border_mode = self.border_mode,
                    rng = rng)
        W, = self.transformer.get_params()
        W.name = 'W'

        self.b = sharedX(self.detector_space.get_origin() + self.init_bias)
        self.b.name = 'b'

        print 'Input shape: ', self.input_space.shape
        print 'Detector space: ', self.detector_space.shape

        assert self.detector_space.num_channels >= 16

        dummy_detector = sharedX(self.detector_space.get_origin_batch(2)[0:16,:,:,:])

        dummy_p = ave_pool_c01b(c01b=dummy_detector, pool_shape=self.pool_shape,
                pool_stride=self.pool_stride,
                image_shape=self.detector_space.shape)
        dummy_p = dummy_p.eval()
        assert self.detector_channels % self.channel_pool_size == 0
        self.output_space = Conv2DSpace(shape=[dummy_p.shape[1], dummy_p.shape[2]],
                num_channels = self.detector_channels // self.channel_pool_size, axes = ('c', 0, 1, 'b') )

        print 'Output space: ', self.output_space.shape

    def censor_updates(self, updates):

        if self.max_kernel_norm is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                row_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=(0,1,2)))
                desired_norms = T.clip(row_norms, 0, self.max_kernel_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + row_norms)).dimshuffle('x', 'x', 'x', 0)

    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        self.b.set_value(biases)

    def get_biases(self):
        return self.b.get_value()

    def get_weights_topo(self):
        return self.transformer.get_weights_topo()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 4

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=(0,1,2)))

        return OrderedDict([
                            ('kernel_norms_min'  , row_norms.min()),
                            ('kernel_norms_mean' , row_norms.mean()),
                            ('kernel_norms_max'  , row_norms.max()),
                            ])

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        state_below = self.input_space.format_as(state_below, self.desired_space)

        if not hasattr(self, 'input_normalization'):
            self.input_normalization = None

        if self.input_normalization:
            state_below = self.input_normalization(state_below)

        # Alex's code requires # input channels to be <= 3 or a multiple of 4
        # so we add dummy channels if necessary
        if not hasattr(self, 'dummy_channels'):
            self.dummy_channels = 0
        if self.dummy_channels > 0:
            state_below = T.concatenate((state_below,
                T.zeros_like(state_below[0:self.dummy_channels, :, :, :])),
                axis=0)

        z = self.transformer.lmul(state_below)
        b = self.b.dimshuffle(0, 1, 2, 'x')


        z = z + b
        if self.layer_name is not None:
            z.name = self.layer_name + '_z'

        self.detector_space.validate(z)

        assert self.detector_space.num_channels % 16 == 0

        if self.output_space.num_channels % 16 == 0:
            # alex's max pool op only works when the number of channels
            # is divisible by 16. we can only do the cross-channel pooling
            # first if the cross-channel pooling preserves that property
            if self.channel_pool_size != 1:
                s = None
                for i in xrange(self.channel_pool_size):
                    t = z[i::self.channel_pool_size,:,:,:]
                    if s is None:
                        s = t
                    else:
                        s = T.maximum(s, t)
                z = s

            p = ave_pool_c01b(c01b=z, pool_shape=self.pool_shape,
                    pool_stride=self.pool_stride,
                    image_shape=self.detector_space.shape)
        else:
            raise NotImplementedError()
            z = max_pool_c01b(c01b=z, pool_shape=self.pool_shape,
                    pool_stride=self.pool_stride,
                    image_shape=self.detector_space.shape)
            if self.channel_pool_size != 1:
                s = None
                for i in xrange(self.channel_pool_size):
                    t = z[i::self.channel_pool_size,:,:,:]
                    if s is None:
                        s = t
                    else:
                        s = T.maximum(s, t)
                z = s
            p = z


        self.output_space.validate(p)

        if not hasattr(self, 'output_normalization'):
            self.output_normalization = None

        if self.output_normalization:
            p = self.output_normalization(p)

        return p

    def get_weights_view_shape(self):
        total = self.detector_channels
        cols = self.channel_pool_size
        if cols == 1:
            # Let the PatchViewer decide how to arrange the units
            # when they're not pooled
            raise NotImplementedError()
        # When they are pooled, make each pooling unit have one row
        rows = total // cols
        if rows * cols < total:
            rows = rows + 1
        return rows, cols

    def get_monitoring_channels_from_state(self, state):

        P = state

        rval = OrderedDict()

        vars_and_prefixes = [ (P,'') ]

        for var, prefix in vars_and_prefixes:
            assert var.ndim == 4
            v_max = var.max(axis=(1,2,3))
            v_min = var.min(axis=(1,2,3))
            v_mean = var.mean(axis=(1,2,3))
            v_range = v_max - v_min

            # max_x.mean_u is "the mean over *u*nits of the max over e*x*amples"
            # The x and u are included in the name because otherwise its hard
            # to remember which axis is which when reading the monitor
            # I use inner.outer rather than outer_of_inner or something like that
            # because I want mean_x.* to appear next to each other in the alphabetical
            # list, as these are commonly plotted together
            for key, val in [
                             ('max_x.max_u', v_max.max()),
                             ('max_x.mean_u', v_max.mean()),
                             ('max_x.min_u', v_max.min()),
                             ('min_x.max_u', v_min.max()),
                             ('min_x.mean_u', v_min.mean()),
                             ('min_x.min_u', v_min.min()),
                             ('range_x.max_u', v_range.max()),
                             ('range_x.mean_u', v_range.mean()),
                             ('range_x.min_u', v_range.min()),
                             ('mean_x.max_u', v_mean.max()),
                             ('mean_x.mean_u', v_mean.mean()),
                             ('mean_x.min_u', v_mean.min())
                             ]:
                rval[prefix+key] = val

        return rval

class SoftmaxOut(Layer):
    """
        A hidden layer that uses the softmax function to do
        max pooling over groups of units.
        When the pooling size is 1, this reduces to a standard
        sigmoidal MLP layer.
    """

    def __init__(self,
                 layer_name,
                 detector_layer_dim,
                 pool_size,
                 pool_stride = None,
                 randomize_pools = False,
                 irange = None,
                 sparse_init = None,
                 sparse_stdev = 1.,
                 include_prob = 1.0,
                 init_bias = 0.,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 max_col_norm = None,
                 max_row_norm = None,
                 mask_weights = None,
                 min_zero = False
        ):
        """

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

            """

        if pool_stride is None:
            pool_stride = pool_size

        self.__dict__.update(locals())
        del self.self

        self.b = sharedX( np.zeros((self.detector_layer_dim,)) + init_bias, name = layer_name + '_b')


        if max_row_norm is not None:
            raise NotImplementedError()

    def get_lr_scalers(self):

        if not hasattr(self, 'W_lr_scale'):
            self.W_lr_scale = None

        if not hasattr(self, 'b_lr_scale'):
            self.b_lr_scale = None

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        if isinstance(space, VectorSpace):
            self.requires_reformat = False
            self.input_dim = space.dim
        else:
            self.requires_reformat = True
            self.input_dim = space.get_total_dimension()
            self.desired_space = VectorSpace(self.input_dim)


        if not ((self.detector_layer_dim - self.pool_size) % self.pool_stride == 0):
            if self.pool_stride == self.pool_size:
                raise ValueError("detector_layer_dim = %d, pool_size = %d. Should be divisible but remainder is %d" %
                             (self.detector_layer_dim, self.pool_size, self.detector_layer_dim % self.pool_size))
            raise ValueError()

        self.h_space = VectorSpace(self.detector_layer_dim)
        self.pool_layer_dim = (self.detector_layer_dim - self.pool_size)/ self.pool_stride + 1
        self.output_space = VectorSpace(self.pool_layer_dim)

        rng = self.mlp.rng
        if self.irange is not None:
            assert self.sparse_init is None
            W = rng.uniform(-self.irange,
                            self.irange,
                            (self.input_dim, self.detector_layer_dim)) * \
                (rng.uniform(0.,1., (self.input_dim, self.detector_layer_dim))
                 < self.include_prob)
        else:
            assert self.sparse_init is not None
            W = np.zeros((self.input_dim, self.detector_layer_dim))
            def mask_rejects(idx, i):
                if self.mask_weights is None:
                    return False
                return self.mask_weights[idx, i] == 0.
            for i in xrange(self.detector_layer_dim):
                assert self.sparse_init <= self.input_dim
                for j in xrange(self.sparse_init):
                    idx = rng.randint(0, self.input_dim)
                    while W[idx, i] != 0 or mask_rejects(idx, i):
                        idx = rng.randint(0, self.input_dim)
                    W[idx, i] = rng.randn()
            W *= self.sparse_stdev

        W = sharedX(W)
        W.name = self.layer_name + '_W'

        self.transformer = MatrixMul(W)

        W ,= self.transformer.get_params()
        assert W.name is not None

        if not hasattr(self, 'randomize_pools'):
            self.randomize_pools = False

        if self.randomize_pools:
            permute = np.zeros((self.detector_layer_dim, self.detector_layer_dim))
            for j in xrange(self.detector_layer_dim):
                i = rng.randint(self.detector_layer_dim)
                permute[i,j] = 1
            self.permute = sharedX(permute)

        if self.mask_weights is not None:
            expected_shape =  (self.input_dim, self.detector_layer_dim)
            if expected_shape != self.mask_weights.shape:
                raise ValueError("Expected mask with shape "+str(expected_shape)+" but got "+str(self.mask_weights.shape))
            self.mask = sharedX(self.mask_weights)

    def censor_updates(self, updates):

        # Patch old pickle files
        if not hasattr(self, 'mask_weights'):
            self.mask_weights = None

        if self.mask_weights is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updates[W] = updates[W] * self.mask

        if self.max_col_norm is not None:
            assert self.max_row_norm is None
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                col_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=0))
                desired_norms = T.clip(col_norms, 0, self.max_col_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + col_norms))

    def get_params(self):
        assert self.b.name is not None
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        assert self.b not in rval
        rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def get_weights(self):
        if self.requires_reformat:
            # This is not really an unimplemented case.
            # We actually don't know how to format the weights
            # in design space. We got the data in topo space
            # and we don't have access to the dataset
            raise NotImplementedError()
        W ,= self.transformer.get_params()
        W = W.get_value()

        if not hasattr(self, 'randomize_pools'):
            self.randomize_pools = False

        if self.randomize_pools:
            warnings.warn("randomize_pools makes get_weights multiply by the permutation matrix. "
                    "If you call set_weights(W) and then call get_weights(), the return value will "
                    "WP not W.")
            P = self.permute.get_value()
            return np.dot(W,P)

        return W

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        self.b.set_value(biases)

    def get_biases(self):
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_view_shape(self):
        total = self.detector_layer_dim
        cols = self.pool_size
        if cols == 1:
            # Let the PatchViewer decide how to arrange the units
            # when they're not pooled
            raise NotImplementedError()
        # When they are pooled, make each pooling unit have one row
        rows = total // cols
        if rows * cols < total:
            rows = rows + 1
        return rows, cols


    def get_weights_topo(self):

        if not isinstance(self.input_space, Conv2DSpace):
            raise NotImplementedError()

        W ,= self.transformer.get_params()

        W = W.T

        W = W.reshape((self.detector_layer_dim, self.input_space.shape[0],
                       self.input_space.shape[1], self.input_space.num_channels))

        W = Conv2DSpace.convert(W, self.input_space.axes, ('b', 0, 1, 'c'))

        return function([], W)()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 2

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=1))
        col_norms = T.sqrt(sq_W.sum(axis=0))

        return OrderedDict([
                            ('row_norms_min'  , row_norms.min()),
                            ('row_norms_mean' , row_norms.mean()),
                            ('row_norms_max'  , row_norms.max()),
                            ('col_norms_min'  , col_norms.min()),
                            ('col_norms_mean' , col_norms.mean()),
                            ('col_norms_max'  , col_norms.max()),
                            ])


    def get_monitoring_channels_from_state(self, state):

        P = state

        rval = OrderedDict()

        if self.pool_size == 1:
            vars_and_prefixes = [ (P,'') ]
        else:
            vars_and_prefixes = [ (P, 'p_') ]

        for var, prefix in vars_and_prefixes:
            v_max = var.max(axis=0)
            v_min = var.min(axis=0)
            v_mean = var.mean(axis=0)
            v_range = v_max - v_min

            # max_x.mean_u is "the mean over *u*nits of the max over e*x*amples"
            # The x and u are included in the name because otherwise its hard
            # to remember which axis is which when reading the monitor
            # I use inner.outer rather than outer_of_inner or something like that
            # because I want mean_x.* to appear next to each other in the alphabetical
            # list, as these are commonly plotted together
            for key, val in [
                             ('max_x.max_u', v_max.max()),
                             ('max_x.mean_u', v_max.mean()),
                             ('max_x.min_u', v_max.min()),
                             ('min_x.max_u', v_min.max()),
                             ('min_x.mean_u', v_min.mean()),
                             ('min_x.min_u', v_min.min()),
                             ('range_x.max_u', v_range.max()),
                             ('range_x.mean_u', v_range.mean()),
                             ('range_x.min_u', v_range.min()),
                             ('mean_x.max_u', v_mean.max()),
                             ('mean_x.mean_u', v_mean.mean()),
                             ('mean_x.min_u', v_mean.min())
                             ]:
                rval[prefix+key] = val

        return rval

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if sb.shape[0] != self.dbm.batch_size:
                        raise ValueError("self.dbm.batch_size is %d but got shape of %d" % (self.dbm.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below) + self.b

        if not hasattr(self, 'randomize_pools'):
            self.randomize_pools = False

        if not hasattr(self, 'pool_stride'):
            self.pool_stride = self.pool_size

        if self.randomize_pools:
            z = T.dot(z, self.permute)

        if not hasattr(self, 'min_zero'):
            self.min_zero = False

        if self.min_zero:
            p = T.zeros_like(z)
        else:
            p = None

        last_start = self.detector_layer_dim  - self.pool_size
        z_pieces = []
        for i in xrange(self.pool_size):
            z_pieces.append(z[:,i:last_start+i+1:self.pool_stride])

        mx = reduce(T.maximum, z_pieces)
        safe_z_pieces = [z_piece - mx for z_piece in z_pieces]

        p_tilde = [T.exp(z_piece) for z_piece in safe_z_pieces]
        denom = sum(p_tilde)
        p = [pt / denom for pt in p_tilde]

        p = sum([z*p for z, p in zip(z_pieces, p)])

        p.name = self.layer_name + '_p_'

        return p

class ZeroMeanChannels(object):
    def __call__(self, c01b):
        return c01b - c01b.mean(axis=(1,2)).dimshuffle(0, 'x', 'x', 1)

def adjust_for_viewer(x):
    return x * 2. - 1.

class make_mnisty(object):

    def apply(self, dataset, can_fit=False):
        topo = dataset.get_topological_view()
        topo = topo[:, 2:-2, 2:-2, :].mean(axis=-1)
        topo = topo.reshape(topo.shape[0], 28, 28, 1)
        topo /= 255.
        assert topo.min() == 0.
        assert topo.max() == 1.
        dataset.set_topological_view(topo)
        dataset.adjust_for_viewer = adjust_for_viewer

# make old imports keep working. this has been moved to forgetting repo though
from forgetting import permute_and_flip

class ExtraChannels(MLP):

    def get_monitoring_channels(self, X, Y = None):

        rval = super(ExtraChannels, self).get_monitoring_channels(X, Y)

        num_samples = 10

        costs = [self.cost_from_X(X,Y) for i in xrange(num_samples)]

        for layer in self.layers:
            for param in layer.get_params():
                grads = [T.grad(cost, param) for cost in costs]
                mean_grad = sum(grads) / float(num_samples)
                centered = [grad - mean_grad for grad in grads]
                squared = [T.sqr(centered) for grad in grads]
                stdev = sum(squared) / float(num_samples)
                stdev = T.sqrt(stdev)
                stdev = stdev.mean()
                rval[layer.layer_name + '_' + param.name+'grad_stdev'] = stdev

        return rval

def redo_first(model):
    rng = np.random.RandomState([1,2,3])
    layer = model.layers[0]
    for param in layer.get_params():
        value = param.get_value()
        new_value = rng.uniform(-.005, .005, value.shape)
        param.set_value(new_value.astype(param.dtype))
    return model

