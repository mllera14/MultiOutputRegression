import numpy as np
import scipy.sparse as ssp
from collections import OrderedDict
from itertools import product

from metrics.score import BGe
from mcmc.graphs.state_space import DAGState
from mcmc.sampling import ProposalDistribution
from structure.graph_generation import random_dag
from core.misc import get_rng, power_set


class ParentSetDistribution:
    """
    This type subclasses CPT to be used as distribution of parent sets for a node.

    Parameters
    ----------
    var: int
        The index of the variable
    parent_sets: list of array like or frozenset
        The possible parent sets of the variable by index
    probabilities: array like of float
        The probability of each parent set. Must be of same length of parent_sets.
    rng: int, RandomState or None (default)
        A random number generator initializer
    """

    def __init__(self, var, parent_sets, probabilities, rng=None):
        if isinstance(var, int):
            var = 'X' + str(var)
        if not isinstance(parent_sets[0], frozenset):
            parent_sets = list(map(lambda x: frozenset(x), parent_sets))

        self.var_name = var
        self.table = OrderedDict(zip(parent_sets, probabilities))
        self.rng = get_rng(rng)

    def __getitem__(self, item):
        return self.table[item]

    @property
    def parent_sets(self):
        return self.table.keys()

    @property
    def log_proba(self):
        return self.table.values()

    def sample(self, condition=None):
        if condition is None:
            table = self.table
        else:
            table = [kv for kv in self.table.items() if condition(kv[0])]

        p_sets, prob = list(zip(*table))

        if len(p_sets) == 1:
            return p_sets[0], prob[0]

        c = max(prob)
        prob = np.exp(prob - c)
        z = prob.sum()

        return self.rng.choice(p_sets, p=prob / z), np.log(z) + c

    def log_z(self, condition=None):
        if condition is None:
            selected = self.table.values()
        else:
            selected = [kv[1] for kv in self.table.items() if condition(kv[0])]

        selected = np.asarray(selected)

        if len(selected) == 1:
            return selected[0]

        c = selected.max()
        return np.log(np.exp(selected - c).sum()) + c


def get_parent_set_distributions(variables, fan_in, score_fn, condition=None, rng=None):
    if isinstance(variables, int):
        n_variables = variables
    elif isinstance(variables, list):
        n_variables = len(variables)
    else:
        raise ValueError("Expected variable list or number of variables")

    rng = get_rng(rng)
    sets = power_set(range(n_variables), fan_in)

    pset_dists = []

    for var in range(n_variables):
        if condition is None:
            var_psets = list(filter(lambda s: var not in s, sets))
        else:
            var_psets = list(filter(lambda s: var not in s and condition(var, s), sets))

        scores = [score_fn((var, ps)) for ps in var_psets]
        psd = ParentSetDistribution(var, var_psets, scores, rng)
        pset_dists.append(psd)

    return pset_dists


class GraphMove:
    @staticmethod
    def moves(state):
        raise NotImplementedError()

    @staticmethod
    def propose(self, state, arcs, scores):
        raise NotImplementedError()


class basic_move(GraphMove):
    @staticmethod
    def moves(state):
        add = ssp.csr_matrix(1 - np.identity(state.shape[0], dtype=np.int))
        add -= state.adj + state.ancestor_matrix

        add = add.tolil()
        add[:, state.adj.A.sum(axis=0) >= state.fan_in_] = 0

        add_edges = list(zip(*add.nonzero()))
        delete_arcs = list(zip(*state.adj.nonzero()))
        
        return add_edges, delete_arcs

    @staticmethod
    def _n_adds(state, fan_in):
        add = ssp.csr_matrix(1 - np.identity(state.shape[0], dtype=np.int))
        add -= state.adj + state.ancestor_matrix

        add = add.tolil()
        add[:, state.adj.A.sum(axis=0) >= fan_in] = 0

        return len(add.nonzero()[0])

    @staticmethod
    def _n_deletes(state):
        delete_arcs = state.adj.nonzero()
        return len(delete_arcs[0])

    @staticmethod
    def propose(state, arcs, scores, rng):
        new_state = state.copy()

        can_add, can_delete = len(arcs[0]), len(arcs[1])
        p = np.asarray([can_add, can_delete]) / (can_add + can_delete)

        # Moves: ADD - 0, DELETE - 1
        move = rng.choice([0, 1], p=p)
        arcs = arcs[move]

        if move:
            # Sample one arc and delete it
            u, v = arcs[rng.choice(can_delete)]
            new_state.remove_edge(u, v)
        else:
            # Else, sample one arc and add it
            u, v = arcs[rng.choice(can_add)]
            new_state.add_edge(u, v)

        # Compute the ratio of the scores
        z_old = scores[v][frozenset(state.adj.parents(v))]
        z_new = scores[v][frozenset(new_state.adj.parents(v))]

        z_ratio = z_new - z_old

        # The probability of the move is the number of neighbors produced by addition and deletion.
        # The probability of the inverse is the same in the new graph
        q_move = can_add + can_delete
        q_inv = basic_move._n_adds(new_state, state.fan_in_) + basic_move._n_deletes(new_state)

        # Return the new state, acceptance ratio and ratio of scores in log space
        return new_state, z_ratio + np.log(q_move / q_inv), z_ratio


class rev_move(GraphMove):
    @staticmethod
    def moves(state: DAGState):
        # All arcs can be reversed
        return list(zip(*state.adj.nonzero()))

    @staticmethod
    def propose(state: DAGState, arcs, scores, rng):
        n = len(arcs)
        i, j = arcs[rng.choice(n)]

        # The descendants of i and j in the current graph
        dsc_i, dsc_j = frozenset(state.descendants(i)), frozenset(state.descendants(j))
        score_old = scores[i][frozenset(state.adj.parents(i))] + scores[j][frozenset(state.adj.parents(j))]

        # Compute the z_score of i excluding it's descendants (including j).
        # Also the z_score* for j excluding it's descendants (i in its parent set)
        z_i = scores[i].log_z(lambda ps: ps.isdisjoint(dsc_i))
        z_star_j = scores[j].log_z(lambda ps: i in ps and ps.isdisjoint(dsc_j))

        new_state = state.copy()
        new_state.orphan([i, j])

        dsc_i = frozenset(new_state.descendants(i))
        ps_i, z_star_i = scores[i].sample(lambda ps: (j in ps) and ps.isdisjoint(dsc_i))

        new_state.add_edges(list(product(ps_i, [i])))

        dsc_j = frozenset(new_state.descendants(j))
        ps_j, z_j = scores[j].sample(lambda ps: ps.isdisjoint(dsc_j))

        new_state.add_edges(list(product(ps_j, [j])))
        score_new = scores[i][frozenset(new_state.adj.parents(i))] + scores[j][frozenset(new_state.adj.parents(j))]

        log_z_ratios = z_star_i + z_j - z_star_j - z_i
        score_diff = score_new - score_old

        return new_state, log_z_ratios + np.log(n / len(new_state.adj.nonzero()[0])), score_diff


class ReattachMove(GraphMove):
    def propose(self, state, arcs, scores):
        pass

    @staticmethod
    def moves(state: DAGState):
        pass


# noinspection PyAttributeOutsideInit
class DAGProposal(ProposalDistribution):
    """
    General proposal distribution over DAGs. This class samples the moves given and passes the current state
    to them so they can propose a new graph from one of its neighbors.

    Parameters
    ----------
    moves: list
        List of instances of graph moves used to propose a new instance.

    move_prob: numpy.ndarray or list
        The probability of choosing each of the moves to perform the proposal.

    score: callable
        A score function used to compute the unnormalized log-probability of parent sets.

    fan_in: int
        The restriction on the maximum number of parents that each node can have.

    prior: callable
        A prior probability on the network structures.

    random_state: numpy.random.RandomState, int or None (default)
        A random number generator or seed used for sampling.

    """
    def __init__(self, moves, move_prob, score=BGe, fan_in=5, prior=None, random_state=None):
        super().__init__(prior=None, random_state=random_state)

        if not all(issubclass(move, GraphMove) for move in moves):
            raise ValueError()

        if len(move_prob) != len(moves):
            raise ValueError('One probability value must be given for each of the moves')

        self.moves = moves
        self.move_prob = np.asarray(move_prob)
        self.score = score
        self.fan_in = fan_in
        # self.scores = ps_scores
        self.prior = prior

    def initialize(self, data, **kwargs):
        variables = data.shape[1]
        score = self.score(data)

        self.ps_scores_ = get_parent_set_distributions(variables, self.fan_in, score, rng=self.rng)
        self.score_fn_ = score
        self.n_variables_ = variables

        return self

    def sample(self, state: DAGState):
        if any(len(state.adj.parents(v)) > self.fan_in for v in state.adj.nodes_iter()):
            raise ValueError(
                'Fan in restriction is {0} but graph has one parent set with bigger size'.format(self.fan_in))

        has_move = []
        move_arcs = []

        state.fan_in_ = self.fan_in

        for m in self.moves:
            moves = m.moves(state)

            has_move.append(bool(len(moves)))
            move_arcs.append(moves)

        move_prob = self.move_prob * has_move
        move_prob /= move_prob.sum()

        m = self.rng.choice(len(self.moves), p=move_prob)

        new_state, acceptance, score_diff = self.moves[m].propose(state, move_arcs[m], self.ps_scores_, self.rng)

        new_state.fan_in_ = self.fan_in

        # Maybe scale the probabilities by how likely it is to make the move?
        # May be necessary if some moves can't be executed in some states.

        return new_state, acceptance, score_diff

    def random_state(self):
        s = DAGState(random_dag(list(range(self.n_variables_)), self.fan_in, self.rng))
        s.fan_in = self.fan_in

        return s