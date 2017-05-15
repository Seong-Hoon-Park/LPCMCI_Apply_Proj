"""Tigramite causal discovery for time series."""

# Author: Jakob Runge <jakobrunge@posteo.de>
#
# License: GNU General Public License v3.0


import numpy
import itertools
from copy import deepcopy
import pickle

try:
    import statsmodels
    from statsmodels.sandbox.stats import multicomp
except:
    print("Could not import statsmodels, p-value corrections not available.")


class PCMCI():
    r"""PCMCI causal discovery for time series datasets.

    PCMCI is a 2-step causal discovery method for large-scale time series
    datasets. The first step is a condition-selection followed by the MCI
    conditional independence test. The implementation is based on Algorithms 1
    and 2 in [1]_.

    PCMCI allows:

       * different conditional independence test statistics adapted to
         continuously-valued or discrete data, and different assumptions about
         linear or nonlinear dependencies
       * hyperparameter optimization
       * easy parallelization
       * handling of masked time series data
       * false discovery control and confidence interval estimation


    Notes 
    ----- 

    .. image:: mci_schematic.*
       :width: 200pt

    In our framework, the dependency structure of a set of time series variables
    is represented in a *time series graph* as shown in the Figure. The nodes of
    a time series graph are defined as the variables at different times and a
    link exists if two lagged variables are *not* conditionally independent
    given the past of the whole process. Assuming stationarity, the links are
    repeated in time. The parents :math:`\mathcal{P}` of a variable are defined
    as the set of all nodes with a link towards it.

    PCMCI is based on a two-step procedure:

    1.  Condition-selection: For all :math:`N` variables: Estimate *superset* of 
        parents :math:`\tilde{\mathcal{P}}(X^j_t)` with iterative PC1 algorithm 
        here implemented as ``run_pc_stable``.


    2.  *Momentary conditional independence* (MCI)

        .. math:: X^i_{t-\tau} ~\perp~ X^j_{t} ~|~ \tilde{\mathcal{P}}(X^j_t),
                                        \tilde{\mathcal{P}}(X^i_{t-{\tau}})\,,
    
    here implemented as ``run_mci``. PCMCI can be flexibly combined with any
    kind of conditional independence test statistic adapted to the kind of data
    (continuous or discrete) and its assumed dependency structure. In Tigramite
    implemented are ParCorr as a linear test, GPACE allowing nonlinear additive
    dependencies, and CMI with different estimators making no assumptions about
    the dependencies.

    The main free parameters of PCMCI (in addition to free parameters of the
    conditional independence test statistic) are the maximum time delay
    :math:`\tau_{\max}` and the significance threshold in the condition-
    selection step :math:`\alpha`. The maximum time delay depends on the
    application and should be chosen according to the maximum causal time lag
    expected in the complex system. We recommend a rather large choice that
    includes peaks in the lagged cross-correlation function (or a more general
    measure). :math:`\alpha` should not be seen as a significance test level in
    the condition-selection step since the iterative hypothesis tests do not
    allow for a precise confidence level. :math:`\alpha` rather takes the role
    of a regularization parameter in model-selection techniques. The
    conditioning sets :math:`\tilde{\mathcal{P}}`  should include the true
    parents and at the same time be small in size to reduce the estimation
    dimension of the MCI test and improve its power. But the first demand is
    typically more important. If a list of values is given or ``pc_alpha=None``,
    this parameter is optimized using model selection criteria.

    Further optional parameters are discussed in [1]_.

    
    Parameters
    ----------
    dataframe : data object
        This can either be the tigramite dataframe object or a pandas data
        frame. It must have the attributes dataframe.values yielding a numpy
        array of shape (observations T, variables N) and optionally a mask of 
        the same shape.

    cond_ind_test : conditional independence test object
        This can be ParCorr or other classes from the tigramite package or an
        external test passed as a callable. This test can be based on the class
        tigramite.independence_tests.CondIndTest. If a callable is passed, it
        must have the signature::

            class CondIndTest():
                # with attributes
                # * measure : str
                #   name of the test
                # * use_mask : bool
                #   whether the mask should be used

                # and functions
                # * run_test(X, Y, Z, tau_max) : where X,Y,Z are of the form 
                #   X = [(var, -tau)]  for non-negative integers var and tau
                #   specifying the variable and time lag
                #   return (test statistic value, p-value)
                # * set_dataframe(dataframe) : set dataframe object

                # optionally also

                # * get_model_selection_criterion(j, parents) : required if
                #   pc_alpha parameter is to be optimized. Here j is the 
                #   variable index and parents a list [(var, -tau), ...]
                #   return score for model selection
                # * get_confidence(X, Y, Z, tau_max) : required for 
                #   return_confidence=True
                #   estimate confidence interval after run_test was called
                #   return (lower bound, upper bound)

    selected_variables : list of integers, optional (default: range(N))
        Specify to estimate parents only for selected variables. If None is
        passed, parents are estimated for all variables.

    var_names : list of strings, optional (default: range(N))
        Names of variables, must match the number of variables. If None is
        passed, variables are enumerated as [0, 1, ...]

    verbosity : int, optional (default: 0)
        Verbose levels 0, 1, ...

    Attributes
    ----------
    all_parents : dictionary    
        Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} containing the
        conditioning-parents estimated with PC algorithm.
    
    estimates : dictionary

        Dictionary of form estimates[j][(i, -tau)] = {'min_val': float,
        'max_pval': float,...} containing the  minimum test statistic value and
        maximum p-value for each link estimated in the PC algorithm.
    
    iterations : dictionary
        Dictionary containing further information on algorithm steps.
    
    N : int
        Number of variables.
    
    T : int
        Time series sample length.

    References
    ----------
    .. [1] J. Runge, D. Sejdinovic, S. Flaxman (2017): Detecting causal
           associations in large nonlinear time series datasets, 
           https://arxiv.org/abs/1702.07007
    """

    def __init__(self, dataframe,
                 cond_ind_test,
                 selected_variables=None,
                 var_names=None,
                 verbosity=0):

        self.dataframe = dataframe
        self.data = dataframe.values
        self.verbosity = verbosity
        self.cond_ind_test = cond_ind_test

        if var_names is None:
            self.var_names = dict([(i, i) for i in range(len(self.data))])
        else:
            self.var_names = var_names

        cond_ind_test.set_dataframe(self.dataframe)

        self.T, self.N = self.data.shape
        if selected_variables is None:
            self.selected_variables = range(self.N)
        else:
            self.selected_variables = selected_variables


        # Some checks
        if selected_variables is not None:
            if (numpy.any(numpy.array(selected_variables) < 0) or
                    numpy.any(numpy.array(selected_variables) >= self.N)):
                raise ValueError("selected_variables must be within 0..N-1")

        if cond_ind_test.use_mask:
            if dataframe.mask is None:
                raise ValueError("dataframe.mask must be array of same shape"
                                 " as fulldata.")
            if type(dataframe.mask) != numpy.ndarray:
                raise TypeError("dataframe.mask is of type %s, " %
                                type(dataframe.mask) +
                                "must be numpy.ndarray")
            if numpy.isnan(dataframe.mask).sum() != 0:
                raise ValueError("NaNs in the sample_selector")

            if self.data.shape != dataframe.mask.shape:
                raise ValueError("shape mismatch: data.shape = %s"
                                 % str(self.data.shape) +
                                 " but data_mask.shape = %s, must identical"
                                 % str(dataframe.mask.shape))

    class _Conditions():
        """Helper class to keep track of conditions.

        Tracks conditions already used for link (i, -tau) --> j

        Parameters
        ----------
        j : int
            Index of current variable.

        parent : tuple
            Tuple of form (i, -tau).

        conds_dim : int
            Cardinality in current step.

        parents_j : list
            List of form [(0, -1), (3, -2), ...]

        Attributes
        ----------
        parents_j_excl_current : list
            List of current parents excluding parent being tested.
        checked_conds : list
            List of already checked condition sets.

         """
        def __init__(self, parent, j, conds_dim, parents_j):

            self.j = j
            self.parent = parent
            self.conds_dim = conds_dim
            self.parents_j_excl_current = [p for p in parents_j if p != parent]
            self.checked_conds = []

        def next_cond(self, check_only=False):
            """Yield next condition.

            Returns next condition from lexicographically ordered conditions.
            Returns False if all possible conditions have been tested.

            Parameters
            ----------
            check_only : bool, default: False
                Return only True instead of next condition.

            Returns
            -------
            cond :  list or bool
                List of form [(0, -1), (3, -2), ...] yielding next condition.
            """
            if len(self.parents_j_excl_current) < self.conds_dim:
                return False

            for cond in itertools.combinations(self.parents_j_excl_current, 
                                                self.conds_dim):

                if set(list(cond)) not in self.checked_conds:
                    if check_only is False:
                        self.checked_conds.append(set(list(cond)))
                        return list(cond)
                    else:
                        return True

            return False

    def _sort_parents(self, test_statistic_values):
        """Sort current parents according to test statistic values.

        Sorting is from strongest to weakest absolute values.

        Paramters
        ---------
        test_statistic_values : dict
            Dictionary of form {(0, -1):float, ...} containing the minimum test
            statistic value of a link

        Returns
        -------
        parents : list
            List of form [(0, -1), (3, -2), ...] containing sorted parents.
        """

        if self.verbosity > 1:
            print("\n    Sorting parents in decreasing order with "
                  "\n    weight(i-tau->j) = min_{iterations} |I_{ij}(tau)| ")

        abs_values = dict([(key, numpy.abs(test_statistic_values[key]))
                           for key in test_statistic_values.keys()])

        parents = sorted(abs_values,
                         key=abs_values.get,
                         reverse=True)

        return parents

    # @profile
    def _run_pc_stable_single(self, j,
                             selected_links=None,
                             tau_min=1,
                             tau_max=1,
                             save_iterations=False,
                             pc_alpha=0.2,
                             max_conds_dim=None,
                             max_combinations=1,
                             ):
        """PC algorithm for estimating parents of single variable.

        Parameters
        ----------
        j : int
            Variable index.
        
        selected_links : list, optional (default: None)
            List of form [(0, -1), (3, -2), ...]
            specifying whether only selected links should be tested. If None is
            passed, all links are tested
        
        tau_min : int, optional (default: 1)
            Minimum time lag to test. Useful for variable selection in 
            multi-step ahead predictions.
        
        tau_max : int, optional (default: 1)
            Maximum time lag. Must be larger or equal to tau_min.
        
        save_iterations : bool, optional (default: False)
            Whether to save iteration step results such as conditions used.
        
        pc_alpha : float or None, optional (default: 0.2)
            Significance level in algorithm. If a list is given, pc_alpha is
            optimized using model selection criteria provided in the
            cond_ind_test class as get_model_selection_criterion(). If None,
            a default list of values is used.
        
        max_conds_dim : int, optional (default: None)
            Maximum number of conditions to test. If None is passed, this number
            is unrestricted.
        
        max_combinations : int, optional (default: 1)
            Maximum number of combinations of conditions of current cardinality
            to test. Defaults to 1 for PC_1 algorithm. For original PC algorithm
            a larger number, such as 10, can be used.

        Returns
        -------
        parents : list
            List of estimated parents.
        
        test_statistic_values : dict
            Dictionary of form {(0, -1):float, ...} containing the minimum test
            statistic value of a link.
        
        p_max : dict
            Dictionary of form {(0, -1):float, ...} containing the maximum 
            p-value of a link across different conditions.
        
        iterations : dict
            Dictionary containing further information on algorithm steps.
        """

        if pc_alpha is None:
            pc_alpha = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5]

        p_max = {}
        test_statistic_values = {}
        parents = selected_links

        iterations = {'iterations': {}}

        #
        # Iteration through increasing number of conditions
        #
        converged = False

        conds_dim = -1   
        while (conds_dim < max_conds_dim):

            conds_dim += 1

            if save_iterations:
                iterations['iterations'][conds_dim] = {}

            # Re-initiate list of non-significant links
            nonsig_parents = []

            if len(parents) - 1 < conds_dim:
                converged = True

            # if converged:
                break

            if self.verbosity > 1:
                print("\nTesting condition sets of dimension"
                      " %d:" % conds_dim)

            parents_here = parents

            # Iterate through all possible pairs (that have not converged yet)
            for ip, parent in enumerate(parents_here):

                if self.verbosity > 1:
                    print("\n    Link (%s %d) --> %s (%d/%d):" % (
                        self.var_names[parent[0]], parent[1], self.var_names[j],
                        ip + 1, len(parents_here)))

                if save_iterations:
                    iterations['iterations'][conds_dim][parent] = {}

                # Initiate conditions class
                conditions = self._Conditions(parent, j, conds_dim, parents)

                comb_index = 0
                while comb_index < max_combinations and conditions.next_cond(
                                                        check_only=True):

                    comb_index += 1

                    # Choose next condition
                    Z = conditions.next_cond()

                    # Perform independence test
                    i, tau = parent

                    val, pval = self.cond_ind_test.run_test(
                        X=[(i, tau)],
                        Y=[(j, 0)],
                        Z=Z,
                        tau_max=tau_max,
                    )

                    if self.verbosity > 1:
                        var_name_Z = ""
                        for Zi in Z:
                            var_name_Z += "(%s %d) " % (
                                self.var_names[Zi[0]], Zi[1])
                        print("    Combination %d: %s --> pval = %.5f /"
                              " val = %.3f" %
                              (comb_index, var_name_Z, pval, val))

                    # Keep track of maximum p-value and minimum estimated value
                    # for each pair (across any condition)
                    if (i, tau) in test_statistic_values.keys():
                        test_statistic_values[(i, tau)] = min(numpy.abs(val),
                                                test_statistic_values[(i, tau)])
                    else:
                        test_statistic_values[(i, tau)] = numpy.abs(val)

                    if (i, tau) in p_max.keys():
                        p_max[(i, tau)] = max(numpy.abs(pval),
                                              p_max[(i, tau)])
                    else:
                        p_max[(i, tau)] = pval

                    if save_iterations:
                        iterations['iterations'][conds_dim][parent][
                                comb_index]={'conds': deepcopy(Z),
                                             'val': val, 'pval': pval}

                    # Delete link later and break while-loop if non-significant
                    if pval > pc_alpha:
                        nonsig_parents.append((j, parent))
                        break

                if self.verbosity > 1:
                    if pval > pc_alpha:
                        print("    Non-significance detected.")
                    elif conditions.next_cond(check_only=True) == False:
                        print("    No conditions of dimension %d left." %
                              conds_dim)
                    else:
                        print("    Still conditions of dimension %d left,"
                              " but q_max = %d reached." % (
                            conds_dim, max_combinations))

            # Remove non-significant links
            for j_parent in nonsig_parents:
                j, parent = j_parent

                del parents[parents.index(parent)]
                del test_statistic_values[parent]

            parents = self._sort_parents(test_statistic_values)

            if self.verbosity > 1:
                print("\nUpdating parents:")
                self._print_parents_single(
                    j, parents, test_statistic_values, p_max)

        if save_iterations:
            iterations['p_max'] = p_max

        if self.verbosity > 1:
            if converged:
                print("\nAlgorithm converged for variable %s" %
                      self.var_names[j])
            else:
                print(
                    "\nAlgorithm not yet converged, but max_conds_dim = %d"
                    " reached." % max_conds_dim)

        return parents, test_statistic_values, p_max, iterations

    def run_pc_stable(self,
                      selected_links=None,
                      tau_min=1,
                      tau_max=1,
                      save_iterations=False,
                      pc_alpha=0.1,
                      max_conds_dim=None,
                      max_combinations=1,
                      ):
        """PC algorithm for estimating parents of all variables.

        Parents are made available as self.all_parents

        Parameters
        ----------
        selected_links : dict or None
            Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} 
            specifying whether only selected links should be tested. If None is
            passed, all links are tested

        tau_min : int, default: 1
            Minimum time lag to test. Useful for multi-step ahead predictions.
        
        tau_max : int, default: 1
            Maximum time lag. Must be larger or equal to tau_min.
        
        save_iterations : bool, default: False
            Whether to save iteration step results such as conditions used.
        
        pc_alpha : float or list of floats, default: 0.1
            Significance level in algorithm. If a list is passed, then the
            pc_alpha level is optimized for every variable across the given 
            pc_alpha values using the score computed in 
            cond_ind_test.get_model_selection_criterion()
        
        max_conds_dim : int or None
            Maximum number of conditions to test. If None is passed, this number
            is unrestricted.
        
        max_combinations : int, default: 1
            Maximum number of combinations of conditions of current cardinality
            to test. Defaults to 1 for PC_1 algorithm. For original PC algorithm
            a larger number, such as 10, can be used.

        Returns
        -------
        all_parents : dict
            Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} 
            containing estimated parents.
        """

        if type(pc_alpha) == float:
            self.alpha_selection = False
        else:
            self.alpha_selection = True

        if tau_min > tau_max or min(tau_min, tau_max) < 0:
            raise ValueError("tau_max = %d, tau_min = %d, " % (
                             tau_max, tau_min)
                             + "but 0 <= tau_min <= tau_max")

        if selected_links is None:
            selected_links = {}
            for j in range(self.N):
                if j in self.selected_variables:
                    selected_links[j] = [(var, -lag)
                                         for var in range(self.N)
                                         for lag in range(tau_min, tau_max + 1)
                                         ]
                else:
                    selected_links[j] = []

        if max_combinations <= 0:
            raise ValueError("max_combinations must be > 0")

        p_max = dict([(j, {}) for j in range(self.N)])
        test_statistic_values = dict([(j, {}) for j in range(self.N)])
        all_parents = selected_links

        iterations = dict([(j, {}) for j in range(self.N)])

        if self.verbosity > 0:
            print("\n##\n## Running Tigramite PC algorithm\n##"
                  "\n\nParameters:")
            print("\nindependence test = %s" % self.cond_ind_test.measure
                  + "\ntau_min = %d" % tau_min
                  + "\ntau_max = %d" % tau_max
                  + "\npc_alpha = %s" % pc_alpha
                  + "\nmax_conds_dim = %s" % max_conds_dim
                  + "\nmax_combinations = %d" % max_combinations)
            print("\n")

        if max_conds_dim is None:
            max_conds_dim = self.N * tau_max

        if max_conds_dim < 0:
            raise ValueError("max_conds_dim must be >= 0")

        for j in self.selected_variables:

            if self.verbosity > 0:
                print("\n## Variable %s" % self.var_names[j])

            if self.alpha_selection == False:
                (all_parents[j],
                 test_statistic_values[j],
                 p_max[j],
                 iterations[j]) = self._run_pc_stable_single(j,
                                            selected_links=selected_links[j],
                                            tau_min=tau_min,
                                            tau_max=tau_max,
                                            save_iterations=save_iterations,
                                            pc_alpha=pc_alpha,
                                            max_conds_dim=max_conds_dim,
                                            max_combinations=max_combinations,
                                            )

            else:
                if self.verbosity > 1:
                    print("\nIterating through pc_alpha = %s:" % pc_alpha)

                score = numpy.zeros(len(pc_alpha))
                results = {}
                for iscore, pc_alpha_here in enumerate(pc_alpha):
                    if self.verbosity > 1:
                        print("\n# pc_alpha = %.3f (%d/%d):" % (pc_alpha_here,
                                            iscore+1, len(pc_alpha)))

                    results[pc_alpha_here] = self._run_pc_stable_single(j,
                                       selected_links=selected_links[j],
                                       tau_min=tau_min,
                                       tau_max=tau_max,
                                       save_iterations=save_iterations,
                                       pc_alpha=pc_alpha_here,
                                       max_conds_dim=max_conds_dim,
                                       max_combinations=max_combinations,
                                       )

                    # Score
                    parents_here = results[pc_alpha_here][0]

                    mscore = self.cond_ind_test.get_model_selection_criterion(j,
                                                     parents_here, tau_max)
                    score[iscore] = mscore

                optimal_alpha = pc_alpha[score.argmin()]

                if self.verbosity > 1:
                    print("\n# Condition selection results:")
                    for iscore, pc_alpha_here in enumerate(pc_alpha):
                        names_parents = "[ "
                        for pari in results[pc_alpha_here][0]:
                            names_parents += "(%s %d) " % (
                                self.var_names[pari[0]], pari[1])
                        names_parents += "]"
                        print("    pc_alpha=%s got score %.4f with parents %s" %
                              (pc_alpha_here, score[iscore], names_parents))
                    print("\n--> optimal pc_alpha for variable %s is %s" %
                          (self.var_names[j], optimal_alpha))

                (all_parents[j],
                 test_statistic_values[j],
                 p_max[j],
                 iterations[j]) = results[optimal_alpha]

                iterations[j]['optimal_pc_alpha'] = optimal_alpha

        # Return results
        estimates = {}
        for j in self.selected_variables:
            estimates[j] = {}
            for parent in all_parents[j]:
                estimates[j][parent] = {
                                    'min_val': test_statistic_values[j][parent],
                                    'max_pval': p_max[j][parent],
                                     }

        self.all_parents = all_parents
        self.estimates = estimates
        self.iterations = iterations

        if self.verbosity > 0:
            print("\n## Resulting condition sets:")
            self._print_parents(all_parents, test_statistic_values, p_max)

        return all_parents

    def _print_parents_single(self, j, parents, test_statistic_values, p_max):
        """Print current parents for variable j.

        Parameters
        ----------
        j : int
            Index of current variable.
        
        parents : list
            List of form [(0, -1), (3, -2), ...]
        
        test_statistic_values : dict
            Dictionary of form {(0, -1):float, ...} containing the minimum test
            statistic value of a link

        p_max : dict
            Dictionary of form {(0, -1):float, ...} containing the maximum 
            p-value of a link across different conditions.

        Returns
        -------
        self : returns an instance of self.
        """
    

        if len(parents) < 20:
            print("\n    Variable %s has %d parent(s):" % (
                            self.var_names[j], len(parents)))
            # if hasattr(self, 'iterations'):
            #     print self.iterations
            if (hasattr(self, 'iterations') 
                and 'optimal_pc_alpha' in self.iterations[j].keys()):
                    print("    [pc_alpha = %s]" % (
                                    self.iterations[j]['optimal_pc_alpha']))    
            for p in parents:
                print("        (%s %d): max_pval = %.5f, min_val = %.3f" % (
                    self.var_names[p[0]], p[1], p_max[p], 
                    test_statistic_values[p]))
        else:
            print("\n    Variable %s has %d parent(s):" % (
                self.var_names[j], len(parents)))

        return self

    def _print_parents(self, all_parents, test_statistic_values, p_max):
        """Print current parents.

        Parameters
        ----------
        all_parents : dictionary    
            Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} containing 
            the conditioning-parents estimated with PC algorithm.

        test_statistic_values : dict
            Dictionary of form {0:{(0, -1):float, ...}} containing the minimum 
            test statistic value of a link

        p_max : dict
            Dictionary of form {0:{(0, -1):float, ...}} containing the maximum 
            p-value of a link across different conditions.

        Returns
        -------
        self : returns an instance of self.
        """
        for j in [var for var in all_parents.keys()]:
            self._print_parents_single(j, all_parents[j],
                                       test_statistic_values[j], p_max[j])
        return self
 
    def get_lagged_dependencies(self,
            selected_links=None,
            tau_min=0,
            tau_max=1,
            parents=None,
            max_conds_py=None,
            max_conds_px=None,
            ):

        """Returns matrix of lagged dependence measure values.

        Parameters
        ----------
        selected_links : dict or None
            Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} 
            specifying whether only selected links should be tested. If None is
            passed, all links are tested

        tau_min : int, default: 1
            Minimum time lag to test. Useful for multi-step ahead predictions.

        tau_max : int, default: 1
            Maximum time lag. Must be larger or equal to tau_min.

        parents : dict or None
            Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} 
            specifying the conditions for each variable. If None is
            passed, no conditions are used.

        max_conds_py : int or None
            Maximum number of conditions of Y to use. If None is passed, this 
            number is unrestricted.

        max_conds_px : int or None
            Maximum number of conditions of Z to use. If None is passed, this 
            number is unrestricted.

        Returns
        -------
        val_matrix : array
            The matrix of shape (N, N, tau_max+1) containing the lagged 
            dependencies.
        """

        if tau_min > tau_max or min(tau_min, tau_max) < 0:
         raise ValueError("tau_max = %d, tau_min = %d, " % (
                          tau_max, tau_min)
                          + "but 0 <= tau_min <= tau_max")

        if selected_links is None:
            selected_links = {}
        
            for j in range(self.N):
                if j in self.selected_variables:
                    selected_links[j] = [(var, -lag)
                                      for var in range(self.N)
                                      for lag in range(tau_min, tau_max + 1)
                                      ]
                else:
                    selected_links[j] = []

        if self.verbosity > 0:
         print("\n## Estimating lagged dependencies")

        if max_conds_py is None:
            max_conds_py = self.N * tau_max

        if max_conds_px is None:
            max_conds_px = self.N * tau_max

        if parents is None:
            parents = {}
            for j in range(self.N):
                parents[j] = []

        val_matrix = numpy.zeros((self.N, self.N, tau_max + 1))

        for j in self.selected_variables:

            conds_y = parents[j][:max_conds_py]

            parent_list = [parent for parent in selected_links[j]
                             if (parent[1] != 0 or parent[0] != j)]

            # Iterate through parents (except those in conditions)
            for cnt, (i, tau) in enumerate(parent_list):

                conds_x = parents[i][:max_conds_px]
                # lag = [-tau]

                if self.verbosity > 1:
                    var_names_condy = "[ "
                    for conds_yi in conds_y:
                        var_names_condy += "(%s %d) " % (
                         self.var_names[conds_yi[0]], conds_yi[1])
                    var_names_condy += "]"
                    var_names_condx = "[ "
                    for conds_xi in conds_x:
                        var_names_condx += "(%s %d) " % (
                         self.var_names[conds_xi[0]], conds_xi[1] + tau)
                    var_names_condx += "]"

                    print("\n        link (%s %d) --> %s (%d/%d):" % (
                        self.var_names[i], tau, self.var_names[j],
                        cnt + 1, len(parent_list)) +
                        "\n        with conds_y = %s" % (var_names_condy) +
                        "\n        with conds_x = %s" % (var_names_condx))

                # Construct lists of tuples for estimating
                # I(X_t-tau; Y_t | Z^Y_t, Z^X_t-tau)
                # with conditions for X shifted by tau
                X = [(i, tau)]
                Y = [(j, 0)]
                Z = conds_y + [(node[0], tau + node[1]) for node in conds_x]

                val = self.cond_ind_test.get_measure(X=X, Y=Y, Z=Z,
                                                     tau_max=tau_max)

                val_matrix[i, j, abs(tau)] = val

                if self.verbosity > 1:
                    self.cond_ind_test._print_cond_ind_results(val=val)

        return val_matrix


    def run_mci(self,
                selected_links=None,
                tau_min=0,
                tau_max=1,
                parents=None,
                max_conds_py=None,
                max_conds_px=None,
                ):

        """MCI conditional independence tests.

        Implements the MCI test (Algorithm 2 in [1]_). Returns the matrices of
        test statistic values,  p-values, and confidence intervals.

        Parameters
        ----------
        selected_links : dict or None
            Dictionary of form {0:all_parents (3, -2), ...], 1:[], ...} 
            specifying whether only selected links should be tested. If None is
            passed, all links are tested

        tau_min : int, default: 1
            Minimum time lag to test. Useful for multi-step ahead predictions.
        
        tau_max : int, default: 1
            Maximum time lag. Must be larger or equal to tau_min.
        
        parents : dict or None
            Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} 
            specifying the conditions for each variable. If None is
            passed, no conditions are used.

        max_conds_py : int or None
            Maximum number of conditions of Y to use. If None is passed, this 
            number is unrestricted.
        
        max_conds_px : int or None
            Maximum number of conditions of Z to use. If None is passed, this 
            number is unrestricted.

        Returns
        -------
        (val_matrix, p_matrix, conf_matrix) : tuple of arrays
            The matrices val_matrix and p_matrix are of shape (N, N, tau_max+1)
            and the matrix conf_matrix is of shape (N, N, tau_max+1, 2)
        """

        if tau_min > tau_max or min(tau_min, tau_max) < 0:
            raise ValueError("tau_max = %d, tau_min = %d, " % (
                             tau_max, tau_min)
                             + "but 0 <= tau_min <= tau_max")

        if selected_links is None:
            selected_links = {}
            for j in range(self.N):
                if j in self.selected_variables:
                    selected_links[j] = [(var, -lag)
                                         for var in range(self.N)
                                         for lag in range(tau_min, tau_max + 1)
                                         ]
                else:
                    selected_links[j] = []

        if self.verbosity > 0:
            print("\n##\n## Running Tigramite MCI algorithm\n##"
                  "\n\nParameters:")

            print("\nindependence test = %s" % self.cond_ind_test.measure
                  + "\ntau_min = %d" % tau_min
                  + "\ntau_max = %d" % tau_max
                  + "\nmax_conds_py = %s" % max_conds_py
                  + "\nmax_conds_px = %s" % max_conds_px)

        if max_conds_py is None:
            max_conds_py = self.N * tau_max

        if max_conds_px is None:
            max_conds_px = self.N * tau_max

        if parents is None:
            parents = {}
            for j in range(self.N):
                parents[j] = []

        val_matrix = numpy.zeros((self.N, self.N, tau_max + 1))
        p_matrix = numpy.ones((self.N, self.N, tau_max + 1))
        conf_matrix = numpy.zeros((self.N, self.N, tau_max + 1, 2))

        for j in self.selected_variables:

            conds_y = parents[j][:max_conds_py]

            parent_list = [parent for parent in selected_links[j]
                         if (parent[1] != 0 or parent[0] != j)]

            # Iterate through parents (except those in conditions)
            for cnt, (i, tau) in enumerate(parent_list):

                conds_x = parents[i][:max_conds_px]
                # lag = [-tau]

                if self.verbosity > 1:
                    var_names_condy = "[ "
                    for conds_yi in conds_y:
                        var_names_condy += "(%s %d) " % (
                            self.var_names[conds_yi[0]], conds_yi[1])
                    var_names_condy += "]"
                    var_names_condx = "[ "
                    for conds_xi in conds_x:
                        var_names_condx += "(%s %d) " % (
                            self.var_names[conds_xi[0]], conds_xi[1] + tau)
                    var_names_condx += "]"

                    print("\n        link (%s %d) --> %s (%d/%d):" % (
                        self.var_names[i], tau, self.var_names[j],
                        cnt + 1, len(parent_list)) +
                          "\n        with conds_y = %s" % (var_names_condy) +
                          "\n        with conds_x = %s" % (var_names_condx))

                # Construct lists of tuples for estimating
                # I(X_t-tau; Y_t | Z^Y_t, Z^X_t-tau)
                # with conditions for X shifted by tau
                X = [(i, tau)]
                Y = [(j, 0)]
                Z = conds_y + [(node[0], tau + node[1]) for node in conds_x]

                val, pval = self.cond_ind_test.run_test(X=X, Y=Y, Z=Z,
                                                        tau_max=tau_max)

                val_matrix[i, j, abs(tau)] = val
                p_matrix[i, j, abs(tau)] = pval

                conf = self.cond_ind_test.get_confidence(X=X, Y=Y, Z=Z,
                                                        tau_max=tau_max)
                conf_matrix[i, j, abs(tau)] = conf

                if self.verbosity > 1:
                    self.cond_ind_test._print_cond_ind_results(val=val, 
                            pval=pval, conf=conf)

        return (val_matrix, p_matrix, conf_matrix)


    def get_corrected_pvalues(self, tau_max,
                              p_matrix,
                              fdr_method='fdr_bh',
                              exclude_contemporaneous=True,
                              ):
        """Returns p-values corrected for multiple testing.
        
        Wrapper around statsmodels.sandbox.stats.multicomp.multipletests.
        Corrections is performed either among all links if
        exclude_contemporaneous==False, or only among lagged links.

        Parameters
        ----------
        tau_max : int
            Maximum time lag.

        p_matrix : array-like
            Matrix of p-values. Must be of shape (N, N, tau_max + 1).

        fdr_method : str, optional (default: 'fdr_bh')
            Correction method, default is Benjamini-Hochberg False Discovery
            Rate method.

        exclude_contemporaneous : bool, optional (default: True)
            Whether to include contemporaneous links in correction.

        Returns
        -------
        q_matrix : array-like
            Matrix of shape (N, N, tau_max + 1) containing corrected p-values.
        """

        if exclude_contemporaneous:
            mask = numpy.ones((self.N, self.N, tau_max + 1), dtype='bool')
            mask[:, :, 0] = False
        else:
            mask = numpy.ones((self.N, self.N, tau_max + 1), dtype='bool')
            mask[range(self.N), range(self.N), 0] = False

        q_matrix = numpy.array(p_matrix)

        if fdr_method != 'none':
            pvals = p_matrix[numpy.where(mask)]
            q_matrix[numpy.where(mask)] = multicomp.multipletests(
                pvals, method=fdr_method)[1]  # .reshape(N,N,tau_max)

        return q_matrix

    def _return_significant_parents(self,
                                  pq_matrix,
                                  val_matrix,
                                  alpha_level=0.05,
                                  ):
        """Returns list of significant parents.

        Significance based on p-matrix, or q-value matrix with corrected
        p-values.

        Parameters
        ----------
        alpha_level : float, optional (default: 0.05)
            Significance level.

        pq_matrix : array-like
            p-matrix, or q-value matrix with corrected p-values. Must be of
            shape (N, N, tau_max + 1).

        val_matrix : array-like
            Matrix of test statistic values. Must be of shape (N, N, tau_max +
            1).

        Returns
        -------
        all_parents : dict
            Dictionary of form {0:[(0, -1), (3, -2), ...], 1:[], ...} 
            containing estimated parents.
        """

        sig_links = (pq_matrix <= alpha_level)
        all_parents = {}
        for j in self.selected_variables:

            links = dict([((p[0], -p[1] - 1), numpy.abs(val_matrix[p[0], 
                            j, abs(p[1]) + 1]))
                          for p in zip(*numpy.where(sig_links[:, j, 1:]))])

            # Sort by value
            all_parents[j] = sorted(links, key=links.get, 
                                                    reverse=True)

        return all_parents

    def _print_significant_parents(self,
                                  p_matrix,
                                  val_matrix,
                                  conf_matrix=None,
                                  q_matrix=None,
                                  alpha_level=0.05,
                                  ):
        """Prints significant parents.

        Parameters
        ----------
        alpha_level : float, optional (default: 0.05)
            Significance level.

        p_matrix : array-like
            Must be of shape (N, N, tau_max + 1).

        val_matrix : array-like
            Must be of shape (N, N, tau_max + 1). 

        q_matrix : array-like, optional (default: None)
            Adjusted p-values. Must be of shape (N, N, tau_max + 1).

        conf_matrix : array-like, optional (default: None)
            Matrix of confidence intervals of shape (N, N, tau_max+1, 2)
        """

        if q_matrix is not None:
            sig_links = (q_matrix <= alpha_level)
        else:
            sig_links = (p_matrix <= alpha_level)

        print("\n## Significant parents at alpha = %.2f:" % alpha_level)
        for j in self.selected_variables:

            links = dict([((p[0], -p[1] - 1), numpy.abs(val_matrix[p[0], 
                            j, abs(p[1]) + 1]))
                          for p in zip(*numpy.where(sig_links[:, j, 1:]))])

            # Sort by value
            sorted_links = sorted(links, key=links.get, reverse=True)

            n_links = len(links)

            string = ""
            if n_links < 20:
                string = ("\n    Variable %s has %d "
                          "parent(s):" % (self.var_names[j], n_links))
                for p in sorted_links:
                    string += ("\n        (%s %d): pval = %.5f" %
                               (self.var_names[p[0]], p[1], 
                                p_matrix[p[0], j, abs(p[1])]))

                    if q_matrix is not None:
                        string += " | qval = %.5f" % (
                            q_matrix[p[0], j, abs(p[1])])

                    string += " | val = %.3f" % (
                        val_matrix[p[0], j, abs(p[1])])

                    if conf_matrix is not None:
                        string += " | conf = (%.3f, %.3f)" % (
                            conf_matrix[p[0], j, abs(p[1])][0], 
                            conf_matrix[p[0], j, abs(p[1])][1])

            else:
                string = ("\n    Variable %s has %d parent(s)" % (
                                self.var_names[j], n_links))
            print string

    # @profile
    def run_pcmci(self,
                  selected_links=None,
                  tau_min=1,
                  tau_max=1,
                  save_iterations=False,
                  pc_alpha=0.05,
                  max_conds_dim=None,
                  max_combinations=1,
                  max_conds_py=None,
                  max_conds_px=None,
                  fdr_method='none',
                  ):

        """Run full PCMCI causal discovery for time series datasets.

        Wrapper around PC-algorithm function and MCI function.

        Parameters
        ----------
        selected_links : list or None
          List of form [(0, -1), (3, -2), ...]
          specifying whether only selected links should be tested. If None is
          passed, all links are tested

        tau_min : int, default: 1
          Minimum time lag to test. Useful for multi-step ahead predictions.

        tau_max : int, default: 1
          Maximum time lag. Must be larger or equal to tau_min.

        save_iterations : bool, default: False
          Whether to save iteration step results such as conditions used.

        pc_alpha : float, default: 0.1
          Significance level in algorithm. 

        max_conds_dim : int or None
          Maximum number of conditions to test. If None is passed, this number
          is unrestricted.

        max_combinations : int, default: 1
          Maximum number of combinations of conditions of current cardinality
          to test. Defaults to 1 for PC_1 algorithm. For original PC algorithm
          a larger number, such as 10, can be used.
      
        max_conds_py : int or None
            Maximum number of conditions of Y to use. If None is passed, this 
            number is unrestricted.

        max_conds_px : int or None
            Maximum number of conditions of Z to use. If None is passed, this 
            number is unrestricted.

        fdr_method : str, optional (default: 'fdr_bh')
            Correction method, default is Benjamini-Hochberg False Discovery
            Rate method.

        Returns
        -------
        (val_matrix, p_matrix, conf_matrix) : tuple of arrays
            The matrices val_matrix and p_matrix are of shape (N, N, tau_max+1)
            and the matrix conf_matrix is of shape (N, N, tau_max+1, 2)
        """

        all_parents = self.run_pc_stable(
            selected_links=selected_links,
            tau_min=tau_min,
            tau_max=tau_max,
            save_iterations=save_iterations,
            pc_alpha=pc_alpha,
            max_conds_dim=max_conds_dim,
            max_combinations=max_combinations,
        )

        # all_parents = self.all_parents

        (val_matrix, p_matrix, conf_matrix) = self.run_mci(
                                                selected_links=selected_links,
                                                tau_min=tau_min,
                                                tau_max=tau_max,
                                                parents=all_parents,
                                                max_conds_py=max_conds_py,
                                                max_conds_px=max_conds_px,
                                                )


        if fdr_method != 'none':
            q_matrix = self.get_corrected_pvalues(tau_max=tau_max,
                                                  p_matrix=p_matrix,
                                                  method=fdr_method)
        else:
            q_matrix = None


        return (val_matrix, p_matrix, conf_matrix, q_matrix)


if __name__ == '__main__':

    import data_processing as pp
    from independence_tests import ParCorr, GPACE

    # numpy.random.seed(40)
    # Example process to play around with
    a = 0.8
    c1 = .8
    c2 = -.8
    c3 = .8
    T = 2500

    # Each key refers to a variable and the incoming links are supplied as a
    # list of format [((driver, lag), coeff), ...]
    links_coeffs = {0: [((0, -1), a), ((1, -1), c1)],
                    1: [((1, -1), a), ((3, -1), c1)],
                    2: [((2, -1), a), ((1, -2), c2), ((3, -3), c3)],
                    3: [((3, -1), a)],
                    }

    data, true_parents_neighbors = pp.var_process(links_coeffs,
                                                  use='inv_inno_cov', T=T)

    data_mask = numpy.zeros(data.shape)

    T, N = data.shape

    var_names = range(N)  # ['X', 'Y', 'Z', 'W']

    pc_alpha = [0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5]
    selected_variables = [2] # [2]  # [2]

    tau_max = 3
    alpha_level = 0.01

    dataframe = pp.DataFrame(data,
        mask=data_mask,
        )
    verbosity = 2

    # import pandas as pd
    # dataframe = pd.DataFrame(data)
    # dataframe.mask = data_mask

    cond_ind_test = ParCorr(
        significance='analytic',
        fixed_thres=0.05,
        sig_samples=100,

        use_mask=False,
        mask_type=['x','y', 'z'],  #  ['x','y','z'],

        confidence='analytic',
        conf_lev=0.9,
        conf_samples=200,
        conf_blocklength=10,

        recycle_residuals=False,
        verbosity=verbosity)

    # cond_ind_test = GPACE(
    #     significance='analytic',
    #     fixed_thres=0.05,
    #     sig_samples=100,

    #     use_mask=False,
    #     mask_type=['y'],

    #     confidence=False,
    #     conf_lev=0.9,
    #     conf_samples=200,
    #     conf_blocklength=None,

    #     gp_version='new',
    #     ace_version='acepack',
    #     recycle_residuals=False,
    #     verbosity=verbosity)

    pcmci = PCMCI(
        dataframe=dataframe,
        cond_ind_test=cond_ind_test,
        selected_variables=selected_variables,
        var_names=var_names,
        verbosity=verbosity)

    pcmci.get_lagged_dependencies(
        selected_links=None,
        tau_min=1,
        tau_max=tau_max,
        parents=None,
        max_conds_py=None,
        max_conds_px=None,
        )

    (val_matrix, p_matrix, conf_matrix, q_matrix) = pcmci.run_pcmci(
        selected_links=None,
        tau_min=1,
        tau_max=tau_max,
        save_iterations=False,

        pc_alpha=pc_alpha,
        max_conds_dim=None,
        max_combinations=1,

        max_conds_py=None,
        max_conds_px=None,

        fdr_method='none',
    )

    pcmci._print_significant_parents(
                   p_matrix=p_matrix,
                   q_matrix=q_matrix, 
                   val_matrix=val_matrix,
                   alpha_level=alpha_level,
                   conf_matrix=conf_matrix)

    # pcmci.run_mci(
    #     selected_links=None,
    #     tau_min=1,
    #     tau_max=tau_max,
    #     parents = None,

    #     max_conds_py=None,
    #     max_conds_px=None,
    # )
    

