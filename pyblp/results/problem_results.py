"""Economy-level structuring of BLP problem results."""

import time
from typing import Any, Callable, Dict, Hashable, List, Optional, Sequence, TYPE_CHECKING, Tuple

import numpy as np
import scipy.linalg

from .results import Results
from .. import exceptions, options
from ..configurations.iteration import Iteration
from ..markets.results_market import ResultsMarket
from ..utilities.algebra import (
    approximately_invert, approximately_solve, compute_condition_number, precisely_compute_eigenvalues
)
from ..utilities.basics import (
    Array, Bounds, Error, SolverStats, format_number, format_seconds, format_table, generate_items, output,
    output_progress
)
from ..utilities.statistics import (
    compute_gmm_moment_covariances, compute_gmm_moments_mean, compute_gmm_parameter_covariances,
    compute_gmm_moments_jacobian_mean, compute_gmm_weights
)


# only import objects that create import cycles when checking types
if TYPE_CHECKING:
    from .bootstrapped_results import BootstrappedResults  # noqa
    from .optimal_instrument_results import OptimalInstrumentResults  # noqa
    from ..economies.problem import Progress  # noqa


class ProblemResults(Results):
    r"""Results of a solved BLP problem.

    Many results are class attributes. Other post-estimation outputs be computed by calling class methods.

    .. note::

       Methods in this class that compute one or more post-estimation output per market support :func:`parallel`
       processing. If multiprocessing is used, market-by-market computation of each post-estimation output will be
       distributed among the processes.

    Attributes
    ----------
    problem : `Problem`
        :class:`Problem` that created these results.
    last_results : `ProblemResults`
        :class:`ProblemResults` from the last GMM step.
    step : `int`
        GMM step that created these results.
    optimization_time : `float`
        Number of seconds it took the optimization routine to finish.
    cumulative_optimization_time : `float`
        Sum of :attr:`ProblemResults.optimization_time` for this step and all prior steps.
    total_time : `float`
        Sum of :attr:`ProblemResults.optimization_time` and the number of seconds it took to set up the GMM step and
        compute results after optimization had finished.
    cumulative_total_time : `float`
        Sum of :attr:`ProblemResults.total_time` for this step and all prior steps.
    converged : `bool`
        Whether the optimization routine converged.
    cumulative_converged : `bool`
        Whether the optimization routine converged for this step and all prior steps.
    optimization_iterations : `int`
        Number of major iterations completed by the optimization routine.
    cumulative_optimization_iterations : `int`
        Sum of :attr:`ProblemResults.optimization_iterations` for this step and all prior steps.
    objective_evaluations : `int`
        Number of GMM objective evaluations.
    cumulative_objective_evaluations : `int`
        Sum of :attr:`ProblemResults.objective_evaluations` for this step and all prior steps.
    fp_converged : `ndarray`
        Flags for convergence of the iteration routine used to compute :math:`\delta(\hat{\theta})` in each market
        during each objective evaluation. Rows are in the same order as :attr:`Problem.unique_market_ids` and column
        indices correspond to objective evaluations.
    cumulative_fp_converged : `ndarray`
        Concatenation of :attr:`ProblemResults.fp_converged` for this step and all prior steps.
    fp_iterations : `ndarray`
        Number of major iterations completed by the iteration routine used to compute :math:`\delta(\hat{\theta})` in
        each market during each objective evaluation. Rows are in the same order as
        :attr:`Problem.unique_market_ids` and column indices correspond to objective evaluations.
    cumulative_fp_iterations : `ndarray`
        Concatenation of :attr:`ProblemResults.fp_iterations` for this step and all prior steps.
    contraction_evaluations : `ndarray`
        Number of times the contraction used to compute :math:`\delta(\hat{\theta})` was evaluated in each market during
        each objective evaluation. Rows are in the same order as :attr:`Problem.unique_market_ids` and column
        indices correspond to objective evaluations.
    cumulative_contraction_evaluations : `ndarray`
        Concatenation of :attr:`ProblemResults.contraction_evaluations` for this step and all prior steps.
    parameters : `ndarray`
        Stacked parameters in the following order: :math:`\hat{\theta}`, concentrated out elements of
        :math:`\hat{\beta}`, and concentrated out elements of :math:`\hat{\gamma}`.
    parameter_covariances : `ndarray`
        Estimated covariance matrix of the stacked parameters, from which standard errors are extracted. Parameter
        covariances are not estimated during the first step of two-step GMM.
    theta : `ndarray`
        Estimated unfixed parameters, :math:`\hat{\theta}`, in the following order: :math:`\hat{\Sigma}`,
        :math:`\hat{\Pi}`, :math:`\hat{\rho}`, non-concentrated out elements from :math:`\hat{\beta}`, and
        non-concentrated out elements from :math:`\hat{\gamma}`.
    sigma : `ndarray`
        Estimated Cholesky root of the covariance matrix for unobserved taste heterogeneity, :math:`\hat{\Sigma}`.
    pi : `ndarray`
        Estimated parameters that measures how agent tastes vary with demographics, :math:`\hat{\Pi}`.
    rho : `ndarray`
        Estimated parameters that measure within nesting group correlations, :math:`\hat{\rho}`.
    beta : `ndarray`
        Estimated demand-side linear parameters, :math:`\hat{\beta}`.
    gamma : `ndarray`
        Estimated supply-side linear parameters, :math:`\hat{\gamma}`.
    sigma_se : `ndarray`
        Estimated standard errors for :math:`\hat{\Sigma}`, which are not estimated in the first step of two-step GMM.
    pi_se : `ndarray`
        Estimated standard errors for :math:`\hat{\Pi}`, which are not estimated in the first step of two-step GMM.
    rho_se : `ndarray`
        Estimated standard errors for :math:`\hat{\rho}`, which are not estimated in the first step of two-step GMM.
    beta_se : `ndarray`
        Estimated standard errors for :math:`\hat{\beta}`, which are not estimated in the first step of two-step GMM.
    gamma_se : `ndarray`
        Estimated standard errors for :math:`\hat{\gamma}`, which are not estimated in the first step of two-step GMM.
    sigma_bounds : `tuple`
        Bounds for :math:`\Sigma` that were used during optimization, which are of the form ``(lb, ub)``.
    pi_bounds : `tuple`
        Bounds for :math:`\Pi` that were used during optimization, which are of the form ``(lb, ub)``.
    rho_bounds : `tuple`
        Bounds for :math:`\rho` that were used during optimization, which are of the form ``(lb, ub)``.
    beta_bounds : `tuple`
        Bounds for :math:`\beta` that were used during optimization, which are of the form ``(lb, ub)``.
    gamma_bounds : `tuple`
        Bounds for :math:`\gamma` that were used during optimization, which are of the form ``(lb, ub)``.
    delta : `ndarray`
        Estimated mean utility, :math:`\delta(\hat{\theta})`.
    tilde_costs : `ndarray`
        Estimated transformed marginal costs, :math:`\tilde{c}(\hat{\theta})` from :eq:`costs`. If ``costs_bounds`` were
        specified in :meth:`Problem.solve`, :math:`c` may have been clipped.
    clipped_costs : `ndarray`
        Vector of booleans indicating whether the associated marginal costs were clipped. All elements will be ``False``
        if ``costs_bounds`` in :meth:`Problem.solve` was not specified.
    xi : `ndarray`
        Estimated unobserved demand-side product characteristics, :math:`\xi(\hat{\theta})`, or equivalently, the
        demand-side structural error term. When there are demand-side fixed effects, this is
        :math:`\Delta\xi(\hat{\theta})` in :eq:`fe`. That is, fixed effects are not included.
    omega : `ndarray`
        Estimated unobserved supply-side product characteristics, :math:`\omega(\hat{\theta})`, or equivalently, the
        supply-side structural error term. When there are supply-side fixed effects, this is
        :math:`\Delta\omega(\hat{\theta})` in :eq:`fe`. That is, fixed effects are not included.
    micro : `ndarray`
        Averaged micro moments, :math:`\bar{g}_M`, in :eq:`averaged_micro_moments`.
    objective : `float`
        GMM objective value, :math:`q(\hat{\theta})`, defined in :eq:`objective`. Note that this expression is scaled
        by a :math:`N^2` term to match numbers traditionally reported in the BLP literature.
    xi_by_theta_jacobian : `ndarray`
        Estimated :math:`\frac{\partial\xi}{\partial\theta} = \frac{\partial\delta}{\partial\theta}`, which is used to
        compute the gradient and standard errors.
    omega_by_theta_jacobian : `ndarray`
        Estimated :math:`\frac{\partial\omega}{\partial\theta} = \frac{\partial\tilde{c}}{\partial\theta}`, which is
        used to compute the gradient and standard errors.
    micro_by_theta_jacobian : `ndarray`
        Estimated :math:`\frac{\partial\bar{g}_M}{\partial\theta}`, which is used to compute the gradient and standard
        errors.
    gradient : `ndarray`
        Gradient of the GMM objective, :math:`\nabla q(\hat{\theta})`, defined in :eq:`gradient`. This is computed after
        the optimization routine finishes even if the routine was configured to not use analytic gradients.
    projected_gradient : `ndarray`
        Projected gradient of the GMM objective. When there are no parameter bounds, this will always be equal to
        :attr:`ProblemResults.gradient`. Otherwise, if an element in :math:`\hat{\theta}` is equal to its lower (upper)
        bound, the corresponding projected gradient value will be truncated at a maximum (minimum) of zero.
    projected_gradient_norm : `ndarray`
        Infinity norm of :attr:`ProblemResults.projected_gradient`.
    hessian : `ndarray`
        Estimated Hessian of the GMM objective. By default, this is computed with finite central differences after the
        optimization routine finishes.
    reduced_hessian : `ndarray`
        Reduced Hessian of the GMM objective. When there are no parameter bounds, this will always be equal to
        :attr:`ProblemResults.hessian`. Otherwise, if an element in :math:`\hat{\theta}` is equal to either its lower
        or upper bound, the corresponding row and column in the reduced Hessian will be all zeros.
    reduced_hessian_eigenvalues : `ndarray`
        Eigenvalues of :attr:`ProblemResults.reduced_hessian`.
    W : `ndarray`
        Weighting matrix, :math:`W`, used to compute these results.
    updated_W : `ndarray`
        Weighting matrix updated according to :eq:`W`.

    Examples
    --------
        - :doc:`Tutorial </tutorial>`

    """

    last_results: Optional['ProblemResults']
    step: int
    optimization_time: float
    cumulative_optimization_time: float
    total_time: float
    cumulative_total_time: float
    converged: bool
    cumulative_converged: bool
    optimization_iterations: int
    cumulative_optimization_iterations: int
    objective_evaluations: int
    cumulative_objective_evaluations: int
    fp_converged: Array
    cumulative_fp_converged: Array
    fp_iterations: Array
    cumulative_fp_iterations: Array
    contraction_evaluations: Array
    cumulative_contraction_evaluations: Array
    parameters: Array
    parameter_covariances: Array
    theta: Array
    sigma: Array
    pi: Array
    rho: Array
    beta: Array
    gamma: Array
    sigma_se: Array
    pi_se: Array
    rho_se: Array
    beta_se: Array
    gamma_se: Array
    sigma_bounds: Bounds
    pi_bounds: Bounds
    rho_bounds: Bounds
    beta_bounds: Bounds
    gamma_bounds: Bounds
    delta: Array
    tilde_costs: Array
    clipped_costs: Array
    xi: Array
    omega: Array
    micro: Array
    objective: Array
    xi_by_theta_jacobian: Array
    omega_by_theta_jacobian: Array
    micro_by_theta_jacobian: Array
    gradient: Array
    projected_gradient: Array
    projected_gradient_norm: Array
    hessian: Array
    reduced_hessian: Array
    reduced_hessian_eigenvalues: Array
    W: Array
    updated_W: Array
    _costs_type: str
    _se_type: str
    _errors: List[Error]

    def __init__(
            self, progress: 'Progress', last_results: Optional['ProblemResults'], last_step: bool,
            step_start_time: float, optimization_start_time: float, optimization_end_time: float,
            optimization_stats: SolverStats, iteration_stats: Sequence[Dict[Hashable, SolverStats]], costs_type: str,
            costs_bounds: Bounds, extra_micro_covariances: Optional[Array], center_moments: bool, W_type: str,
            se_type: str) -> None:
        """Compute cumulative progress statistics, update weighting matrices, and estimate standard errors."""
        super().__init__(progress.problem, progress.parameters, progress.moments)
        self._errors = progress.errors
        self.problem = progress.problem
        self.W = progress.W
        self.theta = progress.theta
        self.delta = progress.delta
        self.tilde_costs = progress.tilde_costs
        self.micro = progress.micro
        self.xi_by_theta_jacobian = progress.xi_jacobian
        self.omega_by_theta_jacobian = progress.omega_jacobian
        self.micro_by_theta_jacobian = progress.micro_jacobian
        self.xi = progress.xi
        self.omega = progress.omega
        self.beta = progress.beta
        self.gamma = progress.gamma
        self.objective = progress.objective
        self.gradient = progress.gradient
        self.projected_gradient = progress.projected_gradient
        self.projected_gradient_norm = progress.projected_gradient_norm
        self.hessian = progress.hessian
        self.reduced_hessian = progress.reduced_hessian
        self.clipped_costs = progress.clipped_costs
        self._costs_type = costs_type
        self._costs_bounds = costs_bounds
        self._se_type = se_type

        # if the reduced Hessian was computed, compute its eigenvalues and the ratio of the smallest to largest ones
        self.reduced_hessian_eigenvalues = np.full(self._parameters.P, np.nan, options.dtype)
        if self._parameters.P > 0 and np.isfinite(self.reduced_hessian).all():
            self.reduced_hessian_eigenvalues, successful = precisely_compute_eigenvalues(self.reduced_hessian)
            if not successful:
                self._errors.append(exceptions.HessianEigenvaluesError(self.reduced_hessian))

        # initialize counts, times, and convergence
        self.step = 1
        self.total_time = self.cumulative_total_time = time.time() - step_start_time
        self.optimization_time = self.cumulative_optimization_time = optimization_end_time - optimization_start_time
        self.converged = self.cumulative_converged = optimization_stats.converged
        self.optimization_iterations = self.cumulative_optimization_iterations = optimization_stats.iterations
        self.objective_evaluations = self.cumulative_objective_evaluations = optimization_stats.evaluations
        self.fp_converged = self.cumulative_fp_converged = np.array(
            [[m[t].converged if m else True for m in iteration_stats] for t in self.problem.unique_market_ids],
            dtype=np.int
        )
        self.fp_iterations = self.cumulative_fp_iterations = np.array(
            [[m[t].iterations if m else 0 for m in iteration_stats] for t in self.problem.unique_market_ids],
            dtype=np.int
        )
        self.contraction_evaluations = self.cumulative_contraction_evaluations = np.array(
            [[m[t].evaluations if m else 0 for m in iteration_stats] for t in self.problem.unique_market_ids],
            dtype=np.int
        )

        # initialize last results and add to cumulative values
        self.last_results = last_results
        if last_results is not None:
            self.step += last_results.step
            self.cumulative_total_time += last_results.cumulative_total_time
            self.cumulative_optimization_time += last_results.cumulative_optimization_time
            self.cumulative_converged = last_results.converged and optimization_stats.converged
            self.cumulative_optimization_iterations += last_results.cumulative_optimization_iterations
            self.cumulative_objective_evaluations += last_results.cumulative_objective_evaluations
            self.cumulative_fp_converged = np.c_[
                last_results.cumulative_fp_converged, self.cumulative_fp_converged
            ]
            self.cumulative_fp_iterations = np.c_[
                last_results.cumulative_fp_iterations, self.cumulative_fp_iterations
            ]
            self.cumulative_contraction_evaluations = np.c_[
                last_results.cumulative_contraction_evaluations, self.cumulative_contraction_evaluations
            ]

        # store estimated parameters and information about them (beta and gamma have already been stored above)
        self.sigma, self.pi, self.rho, _, _ = self._parameters.expand(self.theta)
        self.parameters = np.c_[np.r_[
            self.theta,
            self.beta[self._parameters.eliminated_beta_index],
            self.gamma[self._parameters.eliminated_gamma_index]
        ]]
        self.sigma_bounds = self._parameters.sigma_bounds
        self.pi_bounds = self._parameters.pi_bounds
        self.rho_bounds = self._parameters.rho_bounds
        self.beta_bounds = self._parameters.beta_bounds
        self.gamma_bounds = self._parameters.gamma_bounds

        # ignore computational errors when updating the weighting matrix and computing covariances
        with np.errstate(invalid='ignore'):
            # update the weighting matrix
            micro_covariances = progress.micro_covariances.copy()
            if extra_micro_covariances is not None:
                micro_covariances += extra_micro_covariances
            S_for_weights = self._compute_S(micro_covariances, W_type, center_moments)
            self.updated_W, W_errors = compute_gmm_weights(S_for_weights)
            self._errors.extend(W_errors)

            # only compute parameter covariances and standard errors if this is the last step
            self.parameter_covariances = np.full((self.parameters.size, self.parameters.size), np.nan, options.dtype)
            se = np.full((self.parameters.size, 1), np.nan, options.dtype)
            if last_step:
                S_for_covariances = S_for_weights
                if se_type != W_type or center_moments:
                    S_for_covariances = self._compute_S(micro_covariances, se_type)

                # if this is the first step, an unadjusted weighting matrix needs to be used when computing unadjusted
                #   covariances so that they are scaled properly
                W_for_covariances = self.W
                if se_type == 'unadjusted' and self.step == 1:
                    W_for_covariances, W_for_covariances_errors = compute_gmm_weights(S_for_covariances)
                    self._errors.extend(W_for_covariances_errors)

                # compute parameter covariances
                mean_G = self._compute_mean_G()
                self.parameter_covariances, se_errors = compute_gmm_parameter_covariances(
                    W_for_covariances, S_for_covariances, mean_G, se_type
                )
                self._errors.extend(se_errors)

                # compute standard errors
                with np.errstate(invalid='ignore'):
                    se = np.sqrt(np.c_[self.parameter_covariances.diagonal()] / self.problem.N)
                if np.isnan(se).any():
                    self._errors.append(exceptions.InvalidParameterCovariancesError())

        # expand standard errors
        theta_se, eliminated_beta_se, eliminated_gamma_se = np.split(se, [
            self._parameters.P,
            self._parameters.P + self._parameters.eliminated_beta_index.sum()
        ])
        self.sigma_se, self.pi_se, self.rho_se, self.beta_se, self.gamma_se = (
            self._parameters.expand(theta_se, nullify=True)
        )
        self.beta_se[self._parameters.eliminated_beta_index] = eliminated_beta_se.flatten()
        self.gamma_se[self._parameters.eliminated_gamma_index] = eliminated_gamma_se.flatten()

    def __str__(self) -> str:
        """Format problem results as a string."""
        sections = [self._format_summary(), self._format_cumulative_statistics()]

        # construct a standard error description
        if self._se_type == 'unadjusted':
            se_description = "Unadjusted SEs"
        elif self._se_type == 'robust':
            se_description = "Robust SEs"
        else:
            assert self._se_type == 'clustered'
            se_description = f'Robust SEs Adjusted for {np.unique(self.problem.products.clustering_ids).size} Clusters'

        # add sections formatting estimates and micro moments values
        sections.append(self._parameters.format_estimates(
            f"Estimates ({se_description} in Parentheses)", self.sigma, self.pi, self.rho, self.beta, self.gamma,
            self.sigma_se, self.pi_se, self.rho_se, self.beta_se, self.gamma_se
        ))
        if self._moments.MM > 0:
            sections.append(self._moments.format("Micro Moment Values", self.micro))

        # join the sections into a single string
        return "\n\n".join(sections)

    def _compute_mean_g(self) -> Array:
        """Compute moments."""
        u_list = [self.xi]
        Z_list = [self.problem.products.ZD]
        if self.problem.K3 > 0:
            u_list.append(self.omega)
            Z_list.append(self.problem.products.ZS)
        mean_g = np.r_[compute_gmm_moments_mean(u_list, Z_list), self.micro]
        return mean_g

    def _compute_mean_G(self) -> Array:
        """Compute the Jacobian of moments with respect to parameters."""
        Z_list = [self.problem.products.ZD]
        jacobian_list = [np.c_[
            self.xi_by_theta_jacobian,
            -self.problem.products.X1[:, self._parameters.eliminated_beta_index.flat],
            np.zeros_like(self.problem.products.X3[:, self._parameters.eliminated_gamma_index.flat])
        ]]
        if self.problem.K3 > 0:
            Z_list.append(self.problem.products.ZS)
            jacobian_list.append(np.c_[
                self.omega_by_theta_jacobian,
                np.zeros_like(self.problem.products.X1[:, self._parameters.eliminated_beta_index.flat]),
                -self.problem.products.X3[:, self._parameters.eliminated_gamma_index.flat]
            ])
        mean_G = np.r_[
            compute_gmm_moments_jacobian_mean(jacobian_list, Z_list),
            np.c_[
                self.micro_by_theta_jacobian,
                np.zeros((self._moments.MM, self._parameters.eliminated_beta_index.sum()), options.dtype),
                np.zeros((self._moments.MM, self._parameters.eliminated_gamma_index.sum()), options.dtype)
            ]
        ]
        return mean_G

    def _compute_S(self, micro_covariances: Array, S_type: str, center_moments: bool = False) -> Array:
        """Compute moment covariances."""
        u_list = [self.xi]
        Z_list = [self.problem.products.ZD]
        if self.problem.K3 > 0:
            u_list.append(self.omega)
            Z_list.append(self.problem.products.ZS)
        S = compute_gmm_moment_covariances(u_list, Z_list, S_type, self.problem.products.clustering_ids, center_moments)
        if self._moments.MM > 0:
            S = scipy.linalg.block_diag(S, micro_covariances)
        return S

    def _format_summary(self) -> str:
        """Format a summary table of problem results."""

        # construct the leftmost part of the table that always shows up
        header = [("GMM", "Step"), ("Objective", "Value")]
        values = [self.step, format_number(self.objective)]

        # add information about first and second order conditions
        if np.isfinite(self.projected_gradient_norm):
            if self._parameters.any_bounds:
                header.append(("Projected", "Gradient Norm"))
            else:
                header.append(("Gradient", "Norm"))
            values.append(format_number(self.projected_gradient_norm))
        if np.isfinite(self.reduced_hessian_eigenvalues).any():
            hessian_type = "Reduced" if self._parameters.any_bounds else ""
            if self.reduced_hessian_eigenvalues.size == 1:
                header.append((hessian_type, "Hessian"))
                values.append(format_number(self.reduced_hessian))
            else:
                header.extend([
                    (f"{hessian_type} Hessian", "Min Eigenvalue"),
                    (f"{hessian_type} Hessian", "Max Eigenvalue")
                ])
                values.extend([
                    format_number(self.reduced_hessian_eigenvalues.min()),
                    format_number(self.reduced_hessian_eigenvalues.max())
                ])

        # add a count of any clipped marginal costs
        if np.isfinite(self._costs_bounds).any():
            header.append(("Clipped", "Costs"))
            values.append(self.clipped_costs.sum())

        # add information about the weighting matrix
        header.append(("Weighting Matrix", "Condition Number"))
        values.append(format_number(compute_condition_number(self.W)))

        # format the table
        return format_table(header, values, title="Problem Results Summary")

    def _format_cumulative_statistics(self) -> str:
        """Format a table of cumulative statistics."""

        # construct the leftmost part of the top table that always shows up
        header = [("Computation", "Time")]
        values = [format_seconds(self.cumulative_total_time)]

        # add optimization iterations
        if self._parameters.P > 0:
            header.append(("Optimization", "Iterations"))
            values.append(str(self.cumulative_optimization_iterations))

        # add evaluations and iterations
        header.append(("Objective", "Evaluations"))
        values.append(str(self.cumulative_objective_evaluations))
        if np.any(self.cumulative_contraction_evaluations > 0):
            header.extend([("Fixed Point", "Iterations"), ("Contraction", "Evaluations")])
            values.extend([
                str(self.cumulative_fp_iterations.sum()),
                str(self.cumulative_contraction_evaluations.sum())]
            )

        # format the table
        return format_table(header, values, title="Cumulative Statistics")

    def to_dict(
            self, attributes: Sequence[str] = (
                'step', 'optimization_time', 'cumulative_optimization_time', 'total_time', 'cumulative_total_time',
                'converged', 'cumulative_converged', 'optimization_iterations', 'cumulative_optimization_iterations',
                'objective_evaluations', 'cumulative_objective_evaluations', 'fp_converged', 'cumulative_fp_converged',
                'fp_iterations', 'cumulative_fp_iterations', 'contraction_evaluations',
                'cumulative_contraction_evaluations', 'parameters', 'parameter_covariances', 'theta', 'sigma', 'pi',
                'rho', 'beta', 'gamma', 'sigma_se', 'pi_se', 'rho_se', 'beta_se', 'gamma_se', 'sigma_bounds',
                'pi_bounds', 'rho_bounds', 'beta_bounds', 'gamma_bounds', 'delta', 'tilde_costs', 'clipped_costs', 'xi',
                'omega', 'micro', 'objective', 'xi_by_theta_jacobian', 'omega_by_theta_jacobian',
                'micro_by_theta_jacobian', 'gradient', 'projected_gradient', 'projected_gradient_norm', 'hessian',
                'reduced_hessian', 'reduced_hessian_eigenvalues', 'W', 'updated_W'
            )) -> dict:
        """Convert these results into a dictionary that maps attribute names to values.

        Once converted to a dictionary, these results can be saved to a file with :func:`pickle.dump`.

        Parameters
        ----------
        attributes : `tuple of str, optional`
            Name of attributes that will be added to the dictionary. By default, all :class:`ProblemResults` attributes
            are added except for :attr:`ProblemResults.problem` and :attr:`ProblemResults.last_results`.

        Returns
        -------
        `dict`
            Mapping from attribute names to values.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """
        return {k: getattr(self, k) for k in attributes}

    def run_hansen_test(self) -> float:
        r"""Test the validity of overidentifying restrictions with the Hansen :math:`J` test.

        Following :ref:`references:Hansen (1982)`, the :math:`J` statistic is

        .. math:: J = N\bar{g}(\hat{\theta})'W\bar{g}(\hat{\theta})
           :label: J

        where :math:`\bar{g}(\hat{\theta})` is defined in :eq:`averaged_moments` and :math:`W` is the optimal weighting
        matrix in :eq:`W`.

        .. note::

           The statistic can equivalently be written as :math:`J = q(\hat{\theta}) / N` where the GMM objective value
           is defined in :eq:`objective`.

        When the overidentifying restrictions in this model are valid, the :math:`J` statistic is asymptotically
        :math:`\chi^2` with degrees of freedom equal to the number of overidentifying restrictions. This requires that
        there are more moments than parameters.

        .. warning::

           This test requires :attr:`ProblemResults.W` to be an optimal weighting matrix, so it should typically be run
           only after two-step GMM or after one-step GMM with a pre-specified optimal weighting matrix.

        Returns
        -------
        `float`
            The :math:`J` statistic.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """
        return float(self.objective) / self.problem.N

    def run_distance_test(self, unrestricted: 'ProblemResults') -> float:
        r"""Test the validity of model restrictions with the distance test.

        Following :ref:`references:Newey and West (1987)`, the distance or likelihood ratio-like statistic is

        .. math:: \textit{LR} = J(\hat{\theta^r}) - J(\hat{\theta^u})

        where :math:`J(\hat{\theta^r})` is the :math:`J` statistic defined in :eq:`J` for this restricted model and
        :math:`J(\hat{\theta^u})` is the :math:`J` statistic for the unrestricted model.

        .. note::

           The statistic can equivalently be written as
           :math:`\textit{LR} = [q(\hat{\theta^r}) - q(\hat{\theta^u})] / N` where the GMM objective value is defined in
           :eq:`objective`.

        If the restrictions in this model are valid, the distance statistic is asymptotically :math:`\chi^2` with
        degrees of freedom equal to the number of restrictions.

        .. warning::

           This test requires each model's :attr:`ProblemResults.W` to be the optimal weighting matrix, so it should
           typically be run only after two-step GMM or after one-step GMM with pre-specified optimal weighting matrices.

        Parameters
        ----------
        unrestricted : `ProblemResults`
            :class:`ProblemResults` for the unrestricted model.

        Returns
        -------
        `float`
            The distance statistic.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """

        # validate the other set of results
        if not isinstance(unrestricted, ProblemResults):
            raise TypeError("unrestricted must be another ProblemResults.")
        if unrestricted.problem.N != self.problem.N:
            raise ValueError("unrestricted must have as many observations as these results.")

        # compute the statistic
        return float(self.objective - unrestricted.objective) / self.problem.N

    def run_lm_test(self) -> float:
        r"""Test the validity of model restrictions with the Lagrange multiplier test.

        Following :ref:`references:Newey and West (1987)`, the Lagrange multiplier or score statistic is

        .. math::

           \textit{LM} = N\bar{g}(\hat{\theta})'W\bar{G}(\hat{\theta})V\bar{G}(\hat{\theta})'W\bar{g}(\hat{\theta})

        where :math:`\bar{g}(\hat{\theta})` is defined in :eq:`averaged_moments`, :math:`\bar{G}(\hat{\theta})` is
        defined in :eq:`averaged_moments_jacobian`, :math:`W` is the optimal weighting matrix in :eq:`W`, and :math:`V`
        is the covariance matrix of parameters in :eq:`covariances`.

        If the restrictions in this model are valid, the Lagrange multiplier statistic is asymptotically :math:`\chi^2`
        with degrees of freedom equal to the number of restrictions.

        .. warning::

           This test requires :attr:`ProblemResults.W` to be an optimal weighting matrix, so it should typically be run
           only after two-step GMM or after one-step GMM with a pre-specified optimal weighting matrix.

        Returns
        -------
        `float`
            The Lagrange multiplier statistic.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """
        mean_g = self._compute_mean_g()
        mean_G = self._compute_mean_G()
        gradient = mean_G.T @ self.W @ mean_g
        return self.problem.N * float(gradient.T @ self.parameter_covariances @ gradient)

    def run_wald_test(self, restrictions: Any, restrictions_jacobian: Any) -> float:
        r"""Test the validity of model restrictions with the Wald test.

        Following :ref:`references:Newey and West (1987)`, the Wald statistic is

        .. math:: \text{Wald} = Nr(\hat{\theta})'[R(\hat{\theta})VR(\hat{\theta})']^{-1}r(\hat{\theta})

        where the restrictions are :math:`r(\theta) = 0` under the test's null hypothesis, their Jacobian is
        :math:`R(\theta) = \frac{\partial r(\theta)}{\partial\theta}`, and :math:`V` is the covariance matrix of
        parameters in :eq:`covariances`.

        If the restrictions are valid, the Wald statistic is asymptotically :math:`\chi^2` with degrees of freedom equal
        to the number of restrictions.

        Parameters
        ----------
        restrictions : `array-like`
            Column vector of the model restrictions evaluated at the estimated parameters, :math:`r(\hat{\theta})`.
        restrictions_jacobian : `array-like`
            Estimated Jacobian of the restrictions with respect to all parameters, :math:`R(\hat{\theta})`. This matrix
            should have as many rows as ``restrictions`` and as many columns as
            :attr:`ProblemResults.parameter_covariances`.

        Returns
        -------
        `float`
            The Wald statistic.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """

        # validate the restrictions and their Jacobian
        restrictions = np.c_[np.asarray(restrictions, options.dtype)]
        restrictions_jacobian = np.c_[np.asarray(restrictions_jacobian, options.dtype)]
        if restrictions.shape != (restrictions.shape[0], 1):
            raise ValueError("restrictions must be a column vector.")
        if restrictions_jacobian.shape != (restrictions.shape[0], self.parameter_covariances.shape[0]):
            raise ValueError(
                f"restrictions_jacobian must be a {restrictions.shape[0]} by {self.parameter_covariances.shape[0]} "
                f"matrix."
            )

        # compute the statistic
        matrix = restrictions_jacobian @ self.parameter_covariances @ restrictions_jacobian.T
        inverted, replacement = approximately_invert(matrix)
        if replacement:
            output(exceptions.WaldInversionError(matrix, replacement))
        return self.problem.N * float(restrictions.T @ inverted @ restrictions)

    def bootstrap(
            self, draws: int = 1000, seed: Optional[int] = None, iteration: Optional[Iteration] = None) -> (
            'BootstrappedResults'):
        r"""Use a parametric bootstrap to create an empirical distribution of results.

        The constructed :class:`BootstrappedResults` can be used just like :class:`ProblemResults` to compute various
        post-estimation outputs for different markets. The only difference is that :class:`BootstrappedResults` methods
        return arrays with an extra first dimension, along which bootstrapped results are stacked. These stacked results
        can be used to construct, for example, confidence intervals for post-estimation outputs.

        For each bootstrap draw, parameters are drawn from the estimated multivariate normal distribution of all
        parameters defined by :attr:`ProblemResults.parameters` and :attr:`ProblemResults.parameter_covariances`. Any
        bounds configured in :meth:`Problem.solve` will also bound parameter draws. Each parameter draw is used to
        compute the implied mean utility, :math:`\delta`, and shares, :math:`s`. If a supply side was estimated, the
        implied marginal costs, :math:`c`, and prices, :math:`p`, are computed as well by iterating over the
        :math:`\zeta`-markup contraction in :eq:`zeta_contraction`.

        .. note::

           By default, parametric bootstrapping may use a lot of memory. This is because all bootstrapped results (for
           all ``draws``) are stored in memory at the same time. Memory usage can be reduced by calling this method in a
           loop with ``draws = 1``. In each iteration of the loop, compute the desired post-estimation output with the
           proper method of the returned :class:`BootstrappedResults` class and store these outputs.

        Parameters
        ----------
        draws : `int, optional`
            The number of draws that will be taken from the joint distribution of the parameters. The default value is
            ``1000``.
        seed : `int, optional`
            Passed to :class:`numpy.random.RandomState` to seed the random number generator before any draws are taken.
            By default, a seed is not passed to the random number generator.
        iteration : `Iteration, optional`
            :class:`Iteration` configuration used to compute bootstrapped prices by iterating over the
            :math:`\zeta`-markup equation in :eq:`zeta_contraction`. By default, if a supply side was estimated, this
            is ``Iteration('simple', {'atol': 1e-12})``. Analytic Jacobians are not supported for solving this system.
            This configuration is not used if a supply side was not estimated.

        Returns
        -------
        `BootstrappedResults`
            Computed :class:`BootstrappedResults`.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """
        errors: List[Error] = []

        # keep track of long it takes to bootstrap results
        output("Bootstrapping results ...")
        start_time = time.time()

        # validate the number of draws
        if not isinstance(draws, int) or draws < 1:
            raise ValueError("draws must be a positive int.")

        # validate the iteration configuration
        if self.problem.K3 == 0:
            iteration = None
        elif iteration is None:
            iteration = Iteration('simple', {'atol': 1e-12})
        elif not isinstance(iteration, Iteration):
            raise TypeError("iteration must be None or an iteration instance.")
        elif iteration._compute_jacobian:
            raise ValueError("Analytic Jacobians are not supported for solving this system.")

        # draw from the asymptotic distribution implied by the estimated parameters
        state = np.random.RandomState(seed)
        bootstrapped_parameters = np.atleast_3d(state.multivariate_normal(
            self.parameters.flatten(), self.parameter_covariances, draws
        ))

        # extract the parameters
        bootstrapped_sigma = np.zeros((draws, self.sigma.shape[0], self.sigma.shape[1]), options.dtype)
        bootstrapped_pi = np.zeros((draws, self.pi.shape[0], self.pi.shape[1]), options.dtype)
        bootstrapped_rho = np.zeros((draws, self.rho.shape[0], self.rho.shape[1]), options.dtype)
        bootstrapped_beta = np.zeros((draws, self.beta.shape[0], self.beta.shape[1]), options.dtype)
        bootstrapped_gamma = np.zeros((draws, self.gamma.shape[0], self.gamma.shape[1]), options.dtype)
        bootstrapped_theta, bootstrapped_eliminated_beta, bootstrapped_eliminated_gamma = np.split(
            bootstrapped_parameters,
            [self._parameters.P, self._parameters.P + self._parameters.eliminated_beta_index.sum()],
            axis=1
        )
        bootstrapped_beta[:, self._parameters.eliminated_beta_index.flat] = bootstrapped_eliminated_beta
        bootstrapped_gamma[:, self._parameters.eliminated_gamma_index.flat] = bootstrapped_eliminated_gamma
        for d in range(draws):
            bootstrapped_sigma[d], bootstrapped_pi[d], bootstrapped_rho[d], beta_d, gamma_d = self._parameters.expand(
                bootstrapped_theta[d]
            )
            bootstrapped_beta[d] = np.where(self._parameters.eliminated_beta_index, bootstrapped_beta[d], beta_d)
            bootstrapped_gamma[d] = np.where(self._parameters.eliminated_gamma_index, bootstrapped_gamma[d], gamma_d)
            bootstrapped_sigma[d] = np.clip(bootstrapped_sigma[d], *self.sigma_bounds)
            bootstrapped_pi[d] = np.clip(bootstrapped_pi[d], *self.pi_bounds)
            bootstrapped_rho[d] = np.clip(bootstrapped_rho[d], *self.rho_bounds)
            bootstrapped_beta[d] = np.clip(bootstrapped_beta[d], *self.beta_bounds)
            bootstrapped_gamma[d] = np.clip(bootstrapped_gamma[d], *self.gamma_bounds)

        # compute bootstrapped prices, shares, delta and marginal costs
        bootstrapped_prices = np.zeros((draws, self.problem.N, 1), options.dtype)
        bootstrapped_shares = np.zeros((draws, self.problem.N, 1), options.dtype)
        bootstrapped_delta = np.zeros((draws, self.problem.N, 1), options.dtype)
        bootstrapped_costs = np.zeros((draws, self.problem.N, int(self.problem.K3 > 0)), options.dtype)
        iteration_stats: List[Dict[Hashable, SolverStats]] = []
        for d in output_progress(range(draws), draws, start_time):
            prices_d, shares_d, delta_d, costs_d, iteration_stats_d, errors_d = self._compute_bootstrap(
                iteration, bootstrapped_sigma[d], bootstrapped_pi[d], bootstrapped_rho[d], bootstrapped_beta[d],
                bootstrapped_gamma[d]
            )
            bootstrapped_prices[d] = prices_d
            bootstrapped_shares[d] = shares_d
            bootstrapped_delta[d] = delta_d
            bootstrapped_costs[d] = costs_d
            iteration_stats.append(iteration_stats_d)
            errors.extend(errors_d)

        # structure the results
        from .bootstrapped_results import BootstrappedResults  # noqa
        results = BootstrappedResults(
            self, bootstrapped_sigma, bootstrapped_pi, bootstrapped_rho, bootstrapped_beta, bootstrapped_gamma,
            bootstrapped_prices, bootstrapped_shares, bootstrapped_delta, bootstrapped_costs, start_time, time.time(),
            draws, iteration_stats
        )
        output(f"Bootstrapped results after {format_seconds(results.computation_time)}.")
        output("")
        output(results)
        return results

    def _compute_bootstrap(
            self, iteration: Optional[Iteration], sigma: Array, pi: Array, rho: Array, beta: Array, gamma: Array) -> (
            Tuple[Array, Array, Array, Array, Dict[Hashable, SolverStats], List[Error]]):
        """Compute the equilibrium prices, shares, marginal costs, and delta associated with bootstrapped parameters
        market-by-market
        """
        errors: List[Error] = []

        # compute delta (which will change under equilibrium prices) and marginal costs (which won't change)
        delta = self.delta + self.problem._compute_true_X1() @ (beta - self.beta)
        costs = self.tilde_costs + self.problem._compute_true_X3() @ (gamma - self.gamma)
        if self._costs_type == 'log':
            costs = np.exp(costs)

        # prices will only change if there is an iteration configuration
        prices = self.problem.products.prices if iteration is None else None

        # define a factory for computing bootstrapped prices, shares, and delta in markets
        def market_factory(s: Hashable) -> Tuple[ResultsMarket, Array, Optional[Array], Optional[Iteration]]:
            """Build a market along with arguments used to compute equilibrium prices and shares along with delta."""
            market_s = ResultsMarket(self.problem, s, self._parameters, sigma, pi, rho, beta, delta)
            costs_s = costs[self.problem._product_market_indices[s]]
            prices_s = prices[self.problem._product_market_indices[s]] if prices is not None else None
            return market_s, costs_s, prices_s, iteration

        # compute bootstrapped prices, shares, and delta market-by-market
        equilibrium_prices = np.zeros_like(self.problem.products.prices)
        equilibrium_shares = np.zeros_like(self.problem.products.shares)
        iteration_stats: Dict[Hashable, SolverStats] = {}
        generator = generate_items(
            self.problem.unique_market_ids, market_factory, ResultsMarket.safely_solve_equilibrium_realization
        )
        for t, (prices_t, shares_t, delta_t, iteration_stats_t, errors_t) in generator:
            equilibrium_prices[self.problem._product_market_indices[t]] = prices_t
            equilibrium_shares[self.problem._product_market_indices[t]] = shares_t
            delta[self.problem._product_market_indices[t]] = delta_t
            iteration_stats[t] = iteration_stats_t
            errors.extend(errors_t)

        # return all of the information associated with this bootstrap draw
        return equilibrium_prices, equilibrium_shares, delta, costs, iteration_stats, errors

    def compute_optimal_instruments(
            self, method: str = 'approximate', draws: int = 1, seed: Optional[int] = None,
            expected_prices: Optional[Any] = None, iteration: Optional[Iteration] = None) -> 'OptimalInstrumentResults':
        r"""Estimate feasible optimal or efficient instruments, :math:`Z_D^\textit{Opt}` and :math:`Z_S^\textit{Opt}`.

        Optimal instruments have been shown, for example, by :ref:`references:Reynaert and Verboven (2014)` and
        :ref:`references:Conlon and Gortmaker (2019)`, to reduce bias, improve efficiency, and enhance stability of BLP
        estimates.

        Optimal instruments in the spirit of :ref:`references:Amemiya (1977)` or :ref:`references:Chamberlain (1987)`
        are defined by

        .. math::
           :label: optimal_instruments

           \begin{bmatrix}
               Z_{D,jt}^\textit{Opt} \\
               Z_{S,jt}^\textit{Opt}
           \end{bmatrix}
           = \Sigma_{\xi\omega}^{-1}E\left[
           \begin{matrix}
               \frac{\partial\xi_{jt}}{\partial\theta} \\
               \frac{\partial\omega_{jt}}{\partial\theta}
           \end{matrix}
           \mathrel{\Bigg|} Z \right],

        in which :math:`Z` are all exogenous variables.

        Feasible optimal instruments are estimated by evaluating this expression at an estimated :math:`\hat{\theta}`.
        The expectation is taken by approximating an integral over the joint density of :math:`\xi` and :math:`\omega`.
        For each error term realization, if not already estimated, equilibrium prices and shares are computed by
        iterating over the :math:`\zeta`-markup contraction in :eq:`zeta_contraction`.

        The expected Jacobians are estimated with the average over all computed Jacobian realizations. The
        :math:`2 \times 2` normalizing matrix :math:`\Sigma_{\xi\omega}` is estimated with the sample covariance matrix
        of the error terms.

        Optimal instruments for linear parameters not included in :math:`\theta` are simple product characteristics, so
        they are not computed here but are rather included in the final set of instruments by
        :meth:`OptimalInstrumentResults.to_problem`.

        .. note::

           When both a supply and demand side are estimated, there are usually collinear rows in
           :eq:`optimal_instruments` because of overlapping product characteristics in :math:`X_1` and :math:`X_3`. The
           expression can be corrected by multiplying it with a conformable matrix of ones and zeros that remove the
           collinearity problem. The question of which rows to exclude is addressed in
           :meth:`OptimalInstrumentResults.to_problem`.

        Parameters
        ----------
        method : `str, optional`
            The method by which the integral over the joint density of :math:`\xi` and :math:`\omega` is approximated.
            The following methods are supported:

                - ``'approximate'`` (default) - Evaluate the Jacobians at the expected value of the error terms: zero
                  (``draws`` will be ignored).

                - ``'normal'`` - Draw from the normal approximation to the joint distribution of the error terms and
                  take the average over the computed Jacobians (``draws`` determines the number of draws).

                - ``'empirical'`` - Draw with replacement from the empirical joint distribution of the error terms and
                  take the average over the computed Jacobians (``draws`` determines the number of draws).

        draws : `int, optional`
            The number of draws that will be taken from the joint distribution of the error terms. This is ignored if
            ``method`` is ``'approximate'``. Because the default ``method`` is ``'approximate'``, the default number of
            draws is ``1``, even though it will be ignored. For ``'normal'`` or empirical, larger numbers such as
            ``100`` or ``1000`` are recommended.
        seed : `int, optional`
            Passed to :class:`numpy.random.RandomState` to seed the random number generator before any draws are taken.
            By default, a seed is not passed to the random number generator.
        expected_prices : `array-like, optional`
            Vector of expected prices conditional on all exogenous variables, :math:`E[p \mid Z]`. By default, if a
            supply side was estimated, ``iteration`` is used. If only a demand side was estimated, this is by default
            estimated with the fitted values from a reduced form regression of endogenous prices onto :math:`Z_D`.
        iteration : `Iteration, optional`
            :class:`Iteration` configuration used to estimate expected prices by iterating over the :math:`\zeta`-markup
            contraction in :eq:`zeta_contraction`. By default, if a supply side was estimated, this is
            ``Iteration('simple', {'atol': 1e-12})``. Analytic Jacobians are not supported for solving this system.
            This configuration is not used if ``expected_prices`` is specified.

        Returns
        -------
        `OptimalInstrumentResults`
           Computed :class:`OptimalInstrumentResults`.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """
        errors: List[Error] = []

        # keep track of long it takes to compute optimal instruments for theta
        output("Computing optimal instruments for theta ...")
        start_time = time.time()

        # validate the method and create a function that samples from the error distribution
        if method == 'approximate':
            sample = lambda: (np.zeros_like(self.xi), np.zeros_like(self.omega))
        else:
            state = np.random.RandomState(seed)
            if method == 'normal':
                if self.problem.K3 == 0:
                    variance = np.var(self.xi)
                    sample = lambda: (np.c_[state.normal(0, variance, self.problem.N)], self.omega)
                else:
                    covariances = np.cov(self.xi, self.omega, rowvar=False)
                    sample = lambda: np.hsplit(state.multivariate_normal([0, 0], covariances, self.problem.N), 2)
            elif method == 'empirical':
                if self.problem.K3 == 0:
                    sample = lambda: (self.xi[state.choice(self.problem.N, self.problem.N)], self.omega)
                else:
                    joint = np.c_[self.xi, self.omega]
                    sample = lambda: np.hsplit(joint[state.choice(self.problem.N, self.problem.N)], 2)
            else:
                raise ValueError("method must be 'approximate', 'normal', or 'empirical'.")

        # validate the number of draws (there will be only one for the approximate method)
        if method == 'approximate':
            draws = 1
        if not isinstance(draws, int) or draws < 1:
            raise ValueError("draws must be a positive int.")

        # validate expected prices or their integration configuration (or compute expected prices with a reduced form
        #   regression if unspecified and only a demand side)
        if expected_prices is not None:
            iteration = None
            expected_prices = np.c_[np.asarray(expected_prices, options.dtype)]
            if expected_prices.shape != (self.problem.N, 1):
                raise ValueError(f"expected_prices must be a {self.problem.N}-vector.")
        elif self.problem.K3 > 0:
            if iteration is None:
                iteration = Iteration('simple', {'atol': 1e-12})
            elif not isinstance(iteration, Iteration):
                raise TypeError("iteration must be None or an Iteration instance.")
            elif iteration._compute_jacobian:
                raise ValueError("Analytic Jacobians are not supported for solving this system.")
        else:
            prices = self.problem.products.prices
            if self.problem._absorb_demand_ids is not None:
                prices, absorption_errors = self.problem._absorb_demand_ids(prices)
                errors.extend(absorption_errors)
            covariances = self.problem.products.ZD.T @ self.problem.products.ZD
            parameters, replacement = approximately_solve(covariances, self.problem.products.ZD.T @ prices)
            if replacement:
                errors.append(exceptions.FittedValuesInversionError(covariances, replacement))
            expected_prices = self.problem.products.ZD @ parameters + self.problem.products.prices - prices

        # average over Jacobian realizations
        expected_xi_jacobian = np.zeros_like(self.xi_by_theta_jacobian)
        expected_omega_jacobian = np.zeros_like(self.omega_by_theta_jacobian)
        iteration_stats: List[Dict[Hashable, SolverStats]] = []
        for _ in output_progress(range(draws), draws, start_time):
            xi_jacobian_i, omega_jacobian_i, iteration_stats_i, errors_i = (
                self._compute_realizations(expected_prices, iteration, *sample())
            )
            expected_xi_jacobian += xi_jacobian_i / draws
            expected_omega_jacobian += omega_jacobian_i / draws
            iteration_stats.append(iteration_stats_i)
            errors.extend(errors_i)

        # output a warning about any errors
        if errors:
            output("")
            output(exceptions.MultipleErrors(errors))
            output("")

        # compute the optimal instruments
        if self.problem.K3 == 0:
            inverse_covariance_matrix = np.c_[1 / np.var(self.xi)]
            demand_instruments = inverse_covariance_matrix * expected_xi_jacobian
            supply_instruments = np.full((self.problem.N, 0), np.nan, options.dtype)
        else:
            inverse_covariance_matrix = np.c_[scipy.linalg.inv(np.cov(self.xi, self.omega, rowvar=False))]
            instruments = inverse_covariance_matrix @ np.stack([expected_xi_jacobian, expected_omega_jacobian], axis=1)
            demand_instruments, supply_instruments = np.split(instruments.reshape((self.problem.N, -1)), 2, axis=1)

        # structure the results
        from .optimal_instrument_results import OptimalInstrumentResults  # noqa
        results = OptimalInstrumentResults(
            self, demand_instruments, supply_instruments, inverse_covariance_matrix, expected_xi_jacobian,
            expected_omega_jacobian, expected_prices, start_time, time.time(), draws, iteration_stats
        )
        output(f"Computed optimal instruments after {format_seconds(results.computation_time)}.")
        output("")
        output(results)
        return results

    def _compute_realizations(
            self, expected_prices: Optional[Array], iteration: Optional[Iteration], xi: Array, omega: Array) -> (
            Tuple[Array, Array, Dict[Hashable, SolverStats], List[Error]]):
        """If they have not already been estimated, compute the equilibrium prices, shares, and delta associated with a
        realization of xi and omega market-by-market. Then, compute realizations of Jacobians of xi and omega with
        respect to theta.
        """
        errors: List[Error] = []

        # compute delta (which will change under equilibrium prices) and marginal costs (which won't change)
        delta = self.delta - self.xi + xi
        costs = tilde_costs = self.tilde_costs - self.omega + omega
        if self._costs_type == 'log':
            costs = np.exp(costs)

        # define a factory for computing realizations of prices, shares, and delta in markets
        def market_factory(s: Hashable) -> Tuple[ResultsMarket, Array, Optional[Array], Optional[Iteration]]:
            """Build a market along with arguments used to compute equilibrium prices and shares along with delta."""
            market_s = ResultsMarket(self.problem, s, self._parameters, self.sigma, self.pi, self.rho, self.beta, delta)
            costs_s = costs[self.problem._product_market_indices[s]]
            prices_s = expected_prices[self.problem._product_market_indices[s]] if expected_prices is not None else None
            return market_s, costs_s, prices_s, iteration

        # compute realizations of prices, shares, and delta market-by-market
        equilibrium_prices = np.zeros_like(self.problem.products.prices)
        equilibrium_shares = np.zeros_like(self.problem.products.shares)
        iteration_stats: Dict[Hashable, SolverStats] = {}
        generator = generate_items(
            self.problem.unique_market_ids, market_factory, ResultsMarket.safely_solve_equilibrium_realization
        )
        for t, (prices_t, shares_t, delta_t, iteration_stats_t, errors_t) in generator:
            equilibrium_prices[self.problem._product_market_indices[t]] = prices_t
            equilibrium_shares[self.problem._product_market_indices[t]] = shares_t
            delta[self.problem._product_market_indices[t]] = delta_t
            iteration_stats[t] = iteration_stats_t
            errors.extend(errors_t)

        # compute the Jacobian of xi with respect to theta
        xi_jacobian, demand_errors = self._compute_demand_realization(equilibrium_prices, equilibrium_shares, delta)
        errors.extend(demand_errors)

        # compute the Jacobian of omega with respect to theta
        omega_jacobian = np.full((self.problem.N, self._parameters.P), np.nan, options.dtype)
        if self.problem.K3 > 0:
            omega_jacobian, supply_errors = self._compute_supply_realization(
                equilibrium_prices, equilibrium_shares, delta, tilde_costs, xi_jacobian
            )
            errors.extend(supply_errors)

        # return all of the information associated with this realization
        return xi_jacobian, omega_jacobian, iteration_stats, errors

    def _compute_demand_realization(
            self, equilibrium_prices: Array, equilibrium_shares: Array, delta: Array) -> Tuple[Array, List[Error]]:
        """Compute a realization of the Jacobian of xi with respect to theta market-by-market. If necessary, revert
        problematic elements to their estimated values.
        """
        errors: List[Error] = []

        # check if the Jacobian does not need to be computed
        xi_jacobian = np.full((self.problem.N, self._parameters.P), np.nan, options.dtype)
        if self._parameters.P == 0:
            return xi_jacobian, errors

        # define a factory for computing the Jacobian of xi with respect to theta in markets
        def market_factory(s: Hashable) -> Tuple[ResultsMarket]:
            """Build a market with the data realization along with arguments used to compute the Jacobian."""
            market_s = ResultsMarket(
                self.problem, s, self._parameters, self.sigma, self.pi, self.rho, self.beta, delta, data_override={
                    'prices': equilibrium_prices[self.problem._product_market_indices[s]],
                    'shares': equilibrium_shares[self.problem._product_market_indices[s]]
                }
            )
            return market_s,

        # compute the Jacobian market-by-market
        generator = generate_items(
            self.problem.unique_market_ids, market_factory,
            ResultsMarket.safely_compute_xi_by_theta_jacobian_realization
        )
        for t, (xi_jacobian_t, errors_t) in generator:
            xi_jacobian[self.problem._product_market_indices[t]] = xi_jacobian_t
            errors.extend(errors_t)

        # replace invalid elements
        bad_jacobian_index = ~np.isfinite(xi_jacobian)
        if np.any(bad_jacobian_index):
            xi_jacobian[bad_jacobian_index] = self.xi_by_theta_jacobian[bad_jacobian_index]
            errors.append(exceptions.XiByThetaJacobianReversionError(bad_jacobian_index))
        return xi_jacobian, errors

    def _compute_supply_realization(
            self, equilibrium_prices: Array, equilibrium_shares: Array, delta: Array, tilde_costs: Array,
            xi_jacobian: Array) -> Tuple[Array, List[Error]]:
        """Compute a realization of the Jacobian of omega with respect to theta market-by-market. If necessary, revert
        problematic elements to their estimated values.
        """
        errors: List[Error] = []

        # define a factory for computing the Jacobian of omega with respect to theta in markets
        def market_factory(s: Hashable) -> Tuple[ResultsMarket, Array, Array, str]:
            """Build a market with the data realization along with arguments used to compute the Jacobians."""
            market_s = ResultsMarket(
                self.problem, s, self._parameters, self.sigma, self.pi, self.rho, self.beta, delta, data_override={
                    'prices': equilibrium_prices[self.problem._product_market_indices[s]],
                    'shares': equilibrium_shares[self.problem._product_market_indices[s]]
                }
            )
            tilde_costs_s = tilde_costs[self.problem._product_market_indices[s]]
            xi_jacobian_s = xi_jacobian[self.problem._product_market_indices[s]]
            return market_s, tilde_costs_s, xi_jacobian_s, self._costs_type

        # compute the Jacobian market-by-market
        omega_jacobian = np.full((self.problem.N, self._parameters.P), np.nan, options.dtype)
        generator = generate_items(
            self.problem.unique_market_ids, market_factory,
            ResultsMarket.safely_compute_omega_by_theta_jacobian_realization
        )
        for t, (omega_jacobian_t, errors_t) in generator:
            omega_jacobian[self.problem._product_market_indices[t]] = omega_jacobian_t
            errors.extend(errors_t)

        # the Jacobian should be zero for any clipped marginal costs
        omega_jacobian[self.clipped_costs.flat] = 0

        # replace invalid elements
        bad_jacobian_index = ~np.isfinite(omega_jacobian)
        if np.any(bad_jacobian_index):
            omega_jacobian[bad_jacobian_index] = self.omega_by_theta_jacobian[bad_jacobian_index]
            errors.append(exceptions.OmegaByThetaJacobianReversionError(bad_jacobian_index))
        return omega_jacobian, errors

    def _coerce_matrices(self, matrices: Any, market_ids: Array) -> Array:
        """Coerce array-like stacked matrices into a stacked matrix and validate it."""
        matrices = np.c_[np.asarray(matrices, options.dtype)]
        rows = sum(i.size for t, i in self.problem._product_market_indices.items() if t in market_ids)
        columns = max(i.size for t, i in self.problem._product_market_indices.items() if t in market_ids)
        if matrices.shape != (rows, columns):
            raise ValueError(f"matrices must be {rows} by {columns}.")
        return matrices

    def _coerce_optional_costs(self, costs: Optional[Any], market_ids: Array) -> Array:
        """Coerce optional array-like costs into a column vector and validate it."""
        if costs is not None:
            costs = np.c_[np.asarray(costs, options.dtype)]
            rows = sum(i.size for t, i in self.problem._product_market_indices.items() if t in market_ids)
            if costs.shape != (rows, 1):
                raise ValueError(f"costs must be None or a {rows}-vector.")
        return costs

    def _coerce_optional_prices(self, prices: Optional[Any], market_ids: Array) -> Array:
        """Coerce optional array-like prices into a column vector and validate it."""
        if prices is not None:
            prices = np.c_[np.asarray(prices, options.dtype)]
            rows = sum(i.size for t, i in self.problem._product_market_indices.items() if t in market_ids)
            if prices.shape != (rows, 1):
                raise ValueError(f"prices must be None or a {rows}-vector.")
        return prices

    def _coerce_optional_shares(self, shares: Optional[Any], market_ids: Array) -> Array:
        """Coerce optional array-like shares into a column vector and validate it."""
        if shares is not None:
            shares = np.c_[np.asarray(shares, options.dtype)]
            rows = sum(i.size for t, i in self.problem._product_market_indices.items() if t in market_ids)
            if shares.shape != (rows, 1):
                raise ValueError(f"shares must be None or a {rows}-vector.")
        return shares

    def _combine_arrays(
            self, compute_market_results: Callable, market_ids: Array, fixed_args: Sequence = (),
            market_args: Sequence = ()) -> Array:
        """Compute arrays for one or all markets and stack them into a single matrix. An array for a single market is
        computed by passing fixed_args (identical for all markets) and market_args (matrices with as many rows as there
        are products that are restricted to the market) to compute_market_results, a ResultsMarket method that returns
        the output for the market any errors encountered during computation.
        """
        errors: List[Error] = []

        # keep track of how long it takes to compute the arrays
        start_time = time.time()

        # define a factory for computing arrays in markets
        def market_factory(s: Hashable) -> tuple:
            """Build a market along with arguments used to compute arrays."""
            indices_s = self.problem._product_market_indices[s]
            market_s = ResultsMarket(
                self.problem, s, self._parameters, self.sigma, self.pi, self.rho, self.beta, self.delta, self._moments
            )
            if market_ids.size == 1:
                args_s = market_args
            else:
                args_s = [None if a is None else a[indices_s] for a in market_args]
            return (market_s, *fixed_args, *args_s)

        # construct a mapping from market IDs to market-specific arrays
        matrix_mapping: Dict[Hashable, Array] = {}
        generator = generate_items(market_ids, market_factory, compute_market_results)
        if market_ids.size > 1:
            generator = output_progress(generator, market_ids.size, start_time)
        for t, (array_t, errors_t) in generator:
            matrix_mapping[t] = np.c_[array_t]
            errors.extend(errors_t)

        # output a warning about any errors
        if errors:
            output("")
            output(exceptions.MultipleErrors(errors))
            output("")

        # determine the number of rows and columns
        row_count = sum(matrix_mapping[t].shape[0] for t in market_ids)
        column_count = max(matrix_mapping[t].shape[1] for t in market_ids)

        # preserve the original product order or the sorted market order when stacking the arrays
        combined = np.full((row_count, column_count), np.nan, options.dtype)
        for t, matrix_t in matrix_mapping.items():
            if row_count == market_ids.size:
                combined[market_ids == t, :matrix_t.shape[1]] = matrix_t
            elif row_count == self.problem.N:
                combined[self.problem._product_market_indices[t], :matrix_t.shape[1]] = matrix_t
            else:
                assert market_ids.size == 1
                combined = matrix_t

        # output how long it took to compute the arrays
        end_time = time.time()
        output(f"Finished after {format_seconds(end_time - start_time)}.")
        output("")
        return combined
