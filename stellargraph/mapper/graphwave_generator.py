import tensorflow as tf
from tensorflow.keras import backend as K
import numpy as np
from ..core import StellarGraph
from scipy.sparse.linalg import eigs
from scipy.sparse import diags
from ..core.experimental import experimental


@experimental(reason="lacks unit tests", issues=[815,])
class GraphWaveGenerator:
    """
    Implementation of the GraphWave structural embedding algorithm from the paper:
        "Learning Structural Node Embeddings via Diffusion Wavelets" (https://arxiv.org/pdf/1710.10321.pdf)

    This class is minimally initialized with a StellarGraph object. Calling the flow function will return a tensorflow
    DataSet that contains the GraphWave embeddings.
    """

    def __init__(self, G, scales="auto", num_scales=3, num_eigenvecs=-1):
        """
        Args:
            G (StellarGraph): the StellarGraph object.
            scales (str or list of floats): the wavelet scales to use. "auto" will cause the scale values to be
                automatically calculated.
            num_scales (int): the number of scales when scales = "auto".
            num_eigenvecs (int): the number of eigenvectors to use. When set to `-1` the maximum number of eigenvectors
                is calculated.
        """

        if not isinstance(G, StellarGraph):
            raise TypeError("G must be a StellarGraph object.")

        node_types = list(G.node_types)
        if len(node_types) > 1:
            raise TypeError(
                "{}: node generator requires graph with single node type; "
                "a graph with multiple node types is passed. Stopping.".format(
                    type(self).__name__
                )
            )

        # Create sparse adjacency matrix:
        # Use the node orderings the same as in the graph features
        self.node_list = G.nodes_of_type(node_types[0])
        self.Aadj = G.to_adjacency_matrix(self.node_list)

        # Function to map node IDs to indices for quicker node index lookups
        # TODO: Move this to the graph class
        node_index_dict = dict(zip(self.node_list, range(len(self.node_list))))
        self._node_lookup = np.vectorize(node_index_dict.get, otypes=[np.int64])

        adj = G.to_adjacency_matrix().tocoo()
        degree_mat = diags(np.array(adj.sum(1)).flatten())
        laplacian = degree_mat - adj

        if num_eigenvecs == -1:
            num_eigenvecs = laplacian.shape[0] - 2

        self.eigen_vals, self.eigen_vecs = eigs(laplacian, k=num_eigenvecs)
        self.eigen_vals = np.real(self.eigen_vals).astype(np.float32)
        self.eigen_vecs = np.real(self.eigen_vecs).astype(np.float32)

        if scales == "auto":

            e2 = self.eigen_vals[self.eigen_vals > 0].min()
            eN = self.eigen_vals.max()

            min_scale = -np.log(0.95) / np.sqrt(eN * e2)
            max_scale = -np.log(0.85) / np.sqrt(eN * e2)

            scales = np.linspace(min_scale, max_scale, num_scales)

        self.scales = scales

        # the columns of U exp(-scale * eigenvalues) U^T (U = eigenvectors) are used to calculate the node embeddings
        # (each column corresponds to a node)
        # to avoid computing a dense NxN matrix when only several eigenvalues are specified
        # U exp(-scale * eigenvalues) is computed and stored - which is an N x num_eigenvectors matrix
        # the columns of exp(-scale * eigenvalues) U^T are then computed on the fly in generator.flow()
        self.Ues = [
            self.eigen_vecs.dot(np.diag(np.exp(-s * self.eigen_vals))) for s in scales
        ]  # a list of [U exp(-scale * eigenvalues) for scale in scales]
        self.Ues = tf.convert_to_tensor(np.dstack(self.Ues))

    def flow(
        self, node_ids, sample_points, batch_size, targets=None, repeat=True, threads=1
    ):
        """
        Creates a tensorflow DataSet object of GraphWave embeddings.

        Args:
            node_ids: an iterable of node ids for the nodes of interest
                (e.g., training, validation, or test set nodes)
            sample_points: a 1D array of points at which to sample the characteristic function.
            batch_size: the number of node embeddings to include in a batch.
            targets: a 1D or 2D array of numeric node targets with shape `(len(node_ids)`
                or (len(node_ids), target_size)`
            repeat (bool): indicates whether iterating through the DataSet will continue infinitely or stop after one
                full pass.
            threads (int): number of threads to use.
        """
        ts = tf.convert_to_tensor(sample_points.astype(np.float32))

        dataset = (
            tf.data.Dataset.from_tensor_slices(
                self.eigen_vecs[self._node_lookup(node_ids)]
            )
            .map(  # calculates the columns of U exp(-scale * eigenvalues) U^T on the fly
                lambda x: tf.einsum("ijk,j->ik", self.Ues, x),
                num_parallel_calls=threads,
            )
            .map(  # empirically the characteristic function for each column of U exp(-scale * eigenvalues) U^T
                lambda x: _empirical_characteristic_function(x, ts),
                num_parallel_calls=threads,
            )
        )

        if not targets is None:

            target_dataset = tf.data.Dataset.from_tensor_slices(targets)

            dataset = tf.data.Dataset.zip((dataset, target_dataset))

        # cache embeddings in memory for performance
        if repeat:
            return dataset.cache().batch(batch_size).repeat()
        else:
            return dataset.cache().batch(batch_size)


def _empirical_characteristic_function(samples, ts):
    """
    This function estimates the characteristic function for the wavelet spread of a single node.

    Args:
        samples (Tensor): a tensor of samples drawn from a wavelet distribution at different scales.
        ts (Tensor): a tensor containing the "time" points to sample the characteristic function at.
    Returns:
        embedding (Tensor): the node embedding for the GraphWave algorithm.
    """

    samples = K.expand_dims(samples, 0)  # (ns, scales) -> (1, ns, scales)
    ts = K.expand_dims(K.expand_dims(ts, 1))  # (nt,) -> (nt, 1, 1)

    t_psi = (
        samples * ts
    )  # (1, ns, scales) * (nt, 1, 1) -> (nt, ns, scales) via broadcasting rules

    mean_cos_t_psi = tf.math.reduce_mean(
        tf.math.cos(t_psi), axis=1
    )  # (nt, ns, scales) -> (nt, scales)

    mean_sin_t_psi = tf.math.reduce_mean(
        tf.math.sin(t_psi), axis=1
    )  # (nt, ns, scales) -> (nt, scales)

    # [(nt, scales), (nt, scales)] -> (2 * nt * scales,)
    embedding = K.flatten(tf.concat([mean_cos_t_psi, mean_sin_t_psi], axis=0))

    return embedding