"""Economy-level BLP problem functionality."""

import abc
import collections
import functools
import time
from typing import Any, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import scipy.linalg

from .economy import Economy
from .. import exceptions, options
from ..configurations.formulation import Formulation
from ..configurations.integration import Integration
from ..configurations.iteration import Iteration
from ..configurations.optimization import ObjectiveResults, Optimization
from ..markets.problem_market import ProblemMarket
from ..moments import Moment, EconomyMoments
from ..parameters import Parameters
from ..primitives import Agents, Products
from ..results.problem_results import ProblemResults
from ..utilities.algebra import precisely_invert
from ..utilities.basics import (
    Array, Bounds, Error, Groups, RecArray, SolverStats, format_number, format_seconds, format_table, generate_items,
    output, update_matrices
)
from ..utilities.statistics import IV, compute_gmm_moments_mean, compute_gmm_moments_jacobian_mean


class ProblemEconomy(Economy):
    """An abstract BLP problem."""

    @abc.abstractmethod
    def __init__(
            self, product_formulations: Sequence[Optional[Formulation]], agent_formulation: Optional[Formulation],
            products: RecArray, agents: RecArray) -> None:
        """Initialize the underlying economy with product and agent data."""
        super().__init__(product_formulations, agent_formulation, products, agents)

    def solve(
            self, sigma: Optional[Any] = None, pi: Optional[Any] = None, rho: Optional[Any] = None,
            beta: Optional[Any] = None, gamma: Optional[Any] = None, sigma_bounds: Optional[Tuple[Any, Any]] = None,
            pi_bounds: Optional[Tuple[Any, Any]] = None, rho_bounds: Optional[Tuple[Any, Any]] = None,
            beta_bounds: Optional[Tuple[Any, Any]] = None, gamma_bounds: Optional[Tuple[Any, Any]] = None,
            delta: Optional[Any] = None, method: str = '2s', optimization: Optional[Optimization] = None,
            check_optimality: str = 'both', error_behavior: str = 'revert', error_punishment: float = 1,
            delta_behavior: str = 'first', iteration: Optional[Iteration] = None, fp_type: str = 'safe_linear',
            costs_type: str = 'linear', costs_bounds: Optional[Tuple[Any, Any]] = None, W: Optional[Any] = None,
            center_moments: bool = True, W_type: str = 'robust', se_type: str = 'robust',
            micro_moments: Sequence[Moment] = (), extra_micro_covariances: Optional[Any] = None) -> ProblemResults:
        r"""Solve the problem.

        The problem is solved in one or more GMM steps. During each step, any parameters in :math:`\hat{\theta}` are
        optimized to minimize the GMM objective value. If there are no parameters in :math:`\hat{\theta}` (for example,
        in the logit model there are no nonlinear parameters and all linear parameters can be concentrated out), the
        objective is evaluated once during the step.

        If there are nonlinear parameters, the mean utility, :math:`\delta(\hat{\theta})` is computed market-by-market
        with fixed point iteration. Otherwise, it is computed analytically according to the solution of the logit model.
        If a supply side is to be estimated, marginal costs, :math:`c(\hat{\theta})`, are also computed
        market-by-market. Linear parameters are then estimated, which are used to recover structural error terms, which
        in turn are used to form the objective value. By default, the objective gradient is computed as well.

        .. note::

           This method supports :func:`parallel` processing. If multiprocessing is used, market-by-market computation of
           :math:`\delta(\hat{\theta})` (and :math:`\tilde{c}(\hat{\theta})` if a supply side is estimated), along with
           associated Jacobians, will be distributed among the processes.

        Parameters
        ----------
        sigma : `array-like, optional`
            Configuration for which elements in the Cholesky root of the covariance matrix for unobserved taste
            heterogeneity, :math:`\Sigma`, are fixed at zero and starting values for the other elements, which, if not
            fixed by ``sigma_bounds``, are in the vector of unknown elements, :math:`\theta`.

            Rows and columns correspond to columns in :math:`X_2`, which is formulated according
            ``product_formulations`` in :class:`Problem`. If :math:`X_2` was not formulated, this should not be
            specified, since the logit model will be estimated.

            Values below the diagonal are ignored. Zeros are assumed to be zero throughout estimation and nonzeros are,
            if not fixed by ``sigma_bounds``, starting values for unknown elements in :math:`\theta`. If any columns are
            fixed at zero, only the first few columns of integration nodes (specified in :class:`Problem`) will be used.

        pi : `array-like, optional`
            Configuration for which elements in the matrix of parameters that measures how agent tastes vary with
            demographics, :math:`\Pi`, are fixed at zero and starting values for the other elements, which, if not fixed
            by ``pi_bounds``, are in the vector of unknown elements, :math:`\theta`.

            Rows correspond to the same product characteristics as in ``sigma``. Columns correspond to columns in
            :math:`d`, which is formulated according to ``agent_formulation`` in :class:`Problem`. If :math:`d` was not
            formulated, this should not be specified.

            Zeros are assumed to be zero throughout estimation and nonzeros are, if not fixed by ``pi_bounds``, starting
            values for unknown elements in :math:`\theta`.

        rho : `array-like, optional`
            Configuration for which elements in the vector of parameters that measure within nesting group correlation,
            :math:`\rho`, are fixed at zero and starting values for the other elements, which, if not fixed by
            ``rho_bounds``, are in the vector of unknown elements, :math:`\theta`.

            If this is a scalar, it corresponds to all groups defined by the ``nesting_ids`` field of ``product_data``
            in :class:`Problem`. If this is a vector, it must have :math:`H` elements, one for each nesting group.
            Elements correspond to group IDs in the sorted order of :attr:`Problem.unique_nesting_ids`. If nesting IDs
            were not specified, this should not be specified either.

            Zeros are assumed to be zero throughout estimation and nonzeros are, if not fixed by ``rho_bounds``,
            starting values for unknown elements in :math:`\theta`.

        beta: `array-like, optional`
            Configuration for which elements in the vector of demand-side linear parameters, :math:`\beta`, are
            concentrated out of the problem. Usually, this is left unspecified, unless there is a supply side, in which
            case parameters on endogenous product characteristics cannot be concentrated out of the problem. Values
            specify which elements are fixed at zero and starting values for the other elements, which, if not fixed by
            ``beta_bounds``, are in the vector of unknown elements, :math:`\theta`.

            Elements correspond to columns in :math:`X_1`, which is formulated according to ``product_formulations`` in
            :class:`Problem`.

            Both ``None`` and ``numpy.nan`` indicate that the parameter should be concentrated out of the problem. That
            is, it will be estimated, but does not have to be included in :math:`\theta`. Zeros are assumed to be zero
            throughout estimation and nonzeros are, if not fixed by ``beta_bounds``, starting values for unknown
            elements in :math:`\theta`.

        gamma: `array-like, optional`
            Configuration for which elements in the vector of supply-side linear parameters, :math:`\gamma`, are
            concentrated out of the problem. Usually, this is left unspecified. Values specify which elements are fixed
            at zero and starting values for the other elements, which, if not fixed by ``gamma_bounds``, are in the
            vector of unknown elements, :math:`\theta`.

            Elements correspond to columns in :math:`X_3`, which is formulated according to ``product_formulations`` in
            :class:`Problem`. If :math:`X_3` was not formulated, this should not be specified.

            Both ``None`` and ``numpy.nan`` indicate that the parameter should be concentrated out of the problem. That
            is, it will be estimated, but does not have to be included in :math:`\theta`. Zeros are assumed to be zero
            throughout estimation and nonzeros are, if not fixed by ``gamma_bounds``, starting values for unknown
            elements in :math:`\theta`.

        sigma_bounds : `tuple, optional`
            Configuration for :math:`\Sigma` bounds of the form ``(lb, ub)``, in which both ``lb`` and ``ub`` are of the
            same size as ``sigma``. Each element in ``lb`` and ``ub`` determines the lower and upper bound for its
            counterpart in ``sigma``. If ``optimization`` does not support bounds, these will be ignored.

            By default, if bounds are supported, the diagonal of ``sigma`` is bounded from below by zero. Conditional on
            :math:`X_2`, :math:`\mu`, and an initial estimate of :math:`\mu`, default bounds for off-diagonal parameters
            are chosen to reduce the need for overflow safety precautions.

            Values below the diagonal are ignored. Lower and upper bounds corresponding to zeros in ``sigma`` are set to
            zero. Setting a lower bound equal to an upper bound fixes the corresponding element, removing it from
            :math:`\theta`. Both ``None`` and ``numpy.nan`` are converted to ``-numpy.inf`` in ``lb`` and to
            ``numpy.inf`` in ``ub``.

        pi_bounds : `tuple, optional`
            Configuration for :math:`\Pi` bounds of the form ``(lb, ub)``, in which both ``lb`` and ``ub`` are of the
            same size as ``pi``. Each element in ``lb`` and ``ub`` determines the lower and upper bound for its
            counterpart in ``pi``. If ``optimization`` does not support bounds, these will be ignored.

            By default, if bounds are supported, conditional on :math:`X_2`, :math:`d`, and an initial estimate of
            :math:`\mu`, default bounds are chosen to reduce the need for overflow safety precautions.

            Lower and upper bounds corresponding to zeros in ``pi`` are set to zero. Setting a lower bound equal to an
            upper bound fixes the corresponding element, removing it from :math:`\theta`. Both ``None`` and
            ``numpy.nan`` are converted to ``-numpy.inf`` in ``lb`` and to ``numpy.inf`` in ``ub``.

        rho_bounds : `tuple, optional`
            Configuration for :math:`\rho` bounds of the form ``(lb, ub)``, in which both ``lb`` and ``ub`` are of the
            same size as ``rho``. Each element in ``lb`` and ``ub`` determines the lower and upper bound for its
            counterpart in ``rho``. If ``optimization`` does not support bounds, these will be ignored.

            By default, if bounds are supported, all elements are bounded from below by ``0``, which corresponds to the
            simple logit model. Conditional on an initial estimate of :math:`\mu`, upper bounds are chosen to reduce the
            need for overflow safety precautions, and are less than ``1`` because larger values are inconsistent with
            utility maximization.

            Lower and upper bounds corresponding to zeros in ``rho`` are set to zero. Setting a lower bound equal to an
            upper bound fixes the corresponding element, removing it from :math:`\theta`. Both ``None`` and
            ``numpy.nan`` are converted to ``-numpy.inf`` in ``lb`` and to ``numpy.inf`` in ``ub``.

        beta_bounds : `tuple, optional`
            Configuration for :math:`\beta` bounds of the form ``(lb, ub)``, in which both ``lb`` and ``ub`` are of the
            same size as ``beta``. Each element in ``lb`` and ``ub`` determines the lower and upper bound for its
            counterpart in ``beta``. If ``optimization`` does not support bounds, these will be ignored.

            Usually, this is left unspecified, unless there is a supply side, in which case parameters on endogenous
            product characteristics cannot be concentrated out of the problem. It is generally a good idea to constrain
            such parameters to be nonzero so that the intra-firm Jacobian of shares with respect to prices does not
            become singular.

            By default, all non-concentrated out parameters are unbounded. Bounds should only be specified for
            parameters that are included in :math:`\theta`; that is, those with initial values specified in ``beta``.

            Lower and upper bounds corresponding to zeros in ``beta`` are set to zero. Setting a lower bound equal to an
            upper bound fixes the corresponding element, removing it from :math:`\theta`. Both ``None`` and
            ``numpy.nan`` are converted to ``-numpy.inf`` in ``lb`` and to ``numpy.inf`` in ``ub``.

        gamma_bounds : `tuple, optional`
            Configuration for :math:`\gamma` bounds of the form ``(lb, ub)``, in which both ``lb`` and ``ub`` are of the
            same size as ``gamma``. Each element in ``lb`` and ``ub`` determines the lower and upper bound for its
            counterpart in ``gamma``. If ``optimization`` does not support bounds, these will be ignored.

            By default, all non-concentrated out parameters are unbounded. Bounds should only be specified for
            parameters that are included in :math:`\theta`; that is, those with initial values specified in ``gamma``.

            Lower and upper bounds corresponding to zeros in ``gamma`` are set to zero. Setting a lower bound equal to
            an upper bound fixes the corresponding element, removing it from :math:`\theta`. Both ``None`` and
            ``numpy.nan`` are converted to ``-numpy.inf`` in ``lb`` and to ``numpy.inf`` in ``ub``.

        delta : `array-like, optional`
            Initial values for the mean utility, :math:`\delta`. If there are any nonlinear parameters, these are the
            values at which the fixed point iteration routine will start during the first objective evaluation. By
            default, the solution to the logit model in :eq:`logit_delta` is used. If :math:`\rho` is specified, the
            solution to the nested logit model in :eq:`nested_logit_delta` under the initial ``rho`` is used instead.
        method : `str, optional`
            The estimation routine that will be used. The following methods are supported:

                - ``'1s'`` - One-step GMM.

                - ``'2s'`` (default) - Two-step GMM.

            Iterated GMM can be manually implemented by executing single GMM steps in a loop, in which after the first
            iteration, nonlinear parameters and weighting matrices from the last :class:`ProblemResults` are passed as
            arguments.

        optimization : `Optimization, optional`
            :class:`Optimization` configuration for how to solve the optimization problem in each GMM step, which is
            only used if there are unfixed nonlinear parameters over which to optimize. By default,
            ``Optimization('l-bfgs-b')`` is used. If available, ``Optimization('knitro')`` may be preferable. Generally,
            it is recommended to consider a number of different optimization routines and starting values, verifying
            that :math:`\hat{\theta}` satisfies both the first and second order conditions. Routines that do not support
            bounds will ignore ``sigma_bounds`` and ``pi_bounds``. Choosing a routine that  does not use analytic
            gradients will often down estimation.
        check_optimality : `str, optional`
            How to check for optimality (first and second order conditions) after the optimization routine finishes.
            The following configurations are supported:

                - ``'gradient'`` - Analytically compute the gradient after optimization finishes, but do not compute the
                  Hessian. Since Jacobians needed to compute standard errors will already be computed, gradient
                  computation will not take a long time. This option may be useful if Hessian computation takes a long
                  time when, for example, there are a large number of parameters.

                - ``'both'`` (default) - Also compute the Hessian with central finite differences after optimization
                  finishes. Specifically, analytically compute the gradient :math:`2P` times, perturbing each of the
                  :math:`P` parameters by :math:`\pm\sqrt{\epsilon^\textit{mach}} / 2` where
                  :math:`\epsilon^\textit{mach}` is the machine precision.

        error_behavior : `str, optional`
            How to handle any errors. For example, there can sometimes be overflow or underflow when computing
            :math:`\delta(\hat{\theta})` at a large :math:`\hat{\theta}`. The following behaviors are supported:

                - ``'revert'`` (default) - Revert problematic values to their last computed values. If there are
                  problematic values during the first objective evaluation, revert values in
                  :math:`\delta(\hat{\theta})` to their starting values; in :math:`\tilde{c}(\hat{\theta})`, to prices;
                  in the objective, to ``1e10``; and in other matrices such as Jacobians, to zeros.

                - ``'punish'`` - Set the objective to ``1`` and its gradient to all zeros. This option along with a
                  large ``error_punishment`` can be helpful for routines that do not use analytic gradients.

                - ``'raise'`` - Raise an exception.

        error_punishment : `float, optional`
            How to scale the GMM objective value after an error. By default, the objective value is not scaled.
        delta_behavior : `str, optional`
            Configuration for the values at which the fixed point computation of :math:`\delta(\hat{\theta})` in each
            market will start. This configuration is only relevant if there are unfixed nonlinear parameters over which
            to optimize. The following behaviors are supported:

                - ``'first'`` (default) - Start at the values configured by ``delta`` during the first GMM step, and at
                  the values computed by the last GMM step for each subsequent step.

                - ``'last'`` - Start at the values of :math:`\delta(\hat{\theta})` computed during the last objective
                  evaluation, or, if this is the first evaluation, at the values configured by ``delta``. This behavior
                  tends to speed up computation but may introduce some instability into estimation.

        iteration : `Iteration, optional`
            :class:`Iteration` configuration for how to solve the fixed point problem used to compute
            :math:`\delta(\hat{\theta})` in each market. This configuration is only relevant if there are nonlinear
            parameters, since :math:`\delta` can be estimated analytically in the logit model. By default,
            ``Iteration('squarem', {'atol': 1e-14})`` is used. Newton-based routines such as ``Iteration('lm'`)`` that
            compute the Jacobian can often be faster (especially when there are nesting parameters), but the
            non-Jacobian SQUAREM routine is used by default because it speed is often comparable and in practice it can
            be slightly more stable.
        fp_type : `str, optional`
            Configuration for the type of contraction mapping used to compute :math:`\delta(\hat{\theta})`. The
            following types are supported:

                - ``'safe_linear'`` (default) - The standard linear contraction mapping in :eq:`contraction` (or
                  :eq:`nested_contraction` when there is nesting) with safeguards against numerical overflow.
                  Specifically, :math:`\max_j V_{jti}` (or :math:`\max_j V_{jti} / (1 - \rho_{h(j)})` when there is
                  nesting) is subtracted from :math:`V_{jti}` and the logit expression for choice probabilities in
                  :eq:`probabilities` (or :eq:`nested_probabilities`) is re-scaled accordingly. Such re-scaling is known
                  as the log-sum-exp trick.

                - ``'linear'`` - The standard linear contraction mapping without safeguards against numerical overflow.
                  This option may be preferable to ``'safe_linear'`` if utilities are reasonably small and unlikely to
                  create overflow problems.

                - ``'nonlinear'`` - Iteration over :math:`\exp(\delta_{jt})` instead of :math:`\delta_{jt}`. This can be
                  faster than ``'linear'`` because it involves fewer logarithms. Also, following
                  :ref:`references:Brunner, Heiss, Romahn, and Weiser (2017)`, the :math:`\exp(\delta_{jt})` term can be
                  cancelled out of the expression because it also appears in the numerator of :eq:`probabilities` in the
                  definition of :math:`s_{jt}(\delta, \hat{\theta})`. This second trick only works when there are no
                  nesting parameters.

                - ``'safe_nonlinear'`` - Exponentiated version with minimal safeguards against numerical overflow.
                  Specifically, :math:`\max_j \mu_{jti}` is subtracted from :math:`\mu_{jti}`. This helps with stability
                  but is less helpful than subtracting from the full :math:`V_{jti}`, so this version is less stable
                  than ``'safe_linear'``.

            This option is only relevant if ``sigma`` or ``pi`` are specified because :math:`\delta` can be estimated
            analytically in the logit model with :eq:`logit_delta` and in the nested logit model with
            :eq:`nested_logit_delta`.

        costs_type : `str, optional`
            Specification of the marginal cost function :math:`\tilde{c} = f(c)` in :eq:`costs`. The following
            specifications are supported:

                - ``'linear'`` (default) - Linear specification: :math:`\tilde{c} = c`.

                - ``'log'`` - Log-linear specification: :math:`\tilde{c} = \log c`.

            This specification is only relevant if :math:`X_3` was formulated by ``product_formulations`` in
            :class:`Problem`.

        costs_bounds : `tuple, optional`
            Configuration for :math:`c` bounds of the form ``(lb, ub)``, in which both ``lb`` and ``ub`` are floats.
            This is only relevant if :math:`X_3` was formulated by ``product_formulations`` in :class:`Problem`. By
            default, marginal costs are unbounded.

            When ``costs_type`` is ``'log'``, nonpositive :math:`c(\hat{\theta})` values can create problems when
            computing :math:`\tilde{c}(\hat{\theta}) = \log c(\hat{\theta})`. One solution is to set ``lb`` to a small
            number. Rows in Jacobians associated with clipped marginal costs will be zero.

            Both ``None`` and ``numpy.nan`` are converted to ``-numpy.inf`` in ``lb`` and to ``numpy.inf`` in ``ub``.

        W : `array-like, optional`
            Starting values for the weighting matrix, :math:`W`. By default, the 2SLS weighting matrix in :eq:`2sls_W`
            is used.

            If there are any ``micro_moments``, the initial weighting matrix will by default be block-diagonal with an
            identity matrix for the micro moment block. This micro moment block should usually be replaced by a matrix
            that better reflects micro moment covariances and the size of the micro dataset relative to :math:`N`.

        center_moments : `bool, optional`
            Whether to center each column of the demand- and supply-side moments :math:`g` before updating the weighting
            matrix :math:`W` according to :eq:`W`. By default, the moments are centered. This has no effect if
            ``W_type`` is ``'unadjusted'``.
        W_type : `str, optional`
            How to update the weighting matrix. This has no effect if ``method`` is ``'1s'``. Usually, ``se_type``
            should be the same. The following types are supported:

                - ``'robust'`` (default) - Heteroscedasticity robust weighting matrix defined in :eq:`W` and
                  :eq:`robust_S`.

                - ``'clustered'`` - Clustered weighting matrix defined in :eq:`W` and :eq:`clustered_S`. Clusters must
                  be defined by the ``clustering_ids`` field of ``product_data`` in :class:`Problem`.

                - ``'unadjusted'`` - Homoskedastic weighting matrix defined in :eq:`W` and :eq:`unadjusted_S`.

            This only affects the standard demand- and supply-side block of the updated weighting matrix. If there are
            micro moments, this matrix will be block-diagonal with a micro moment block equal to the inverse of the
            covariance matrix defined in :eq:`averaged_micro_moment_covariances` plus any ``extra_micro_covariances``.

        se_type : `str, optional`
            How to compute parameter covarainces and standard errors. Usually, ``W_type`` should be the same. The
            following types are supported:

                - ``'robust'`` (default) - Heteroscedasticity robust covariances defined in :eq:`covariances` and
                  :eq:`robust_S`.

                - ``'clustered'`` - Clustered covariances defined in :eq:`covariances` and :eq:`clustered_S`. Clusters
                  must be defined by the ``clustering_ids`` field of ``product_data`` in :class:`Problem`.

                - ``'unadjusted'`` - Homoskedastic covariances defined in :eq:`unadjusted_covariances`, which are
                  computed under the assumption that the weighting matrix is optimal.

            This only affects the standard demand- and supply-side block of the matrix of averaged moment covariances.
            If there are micro moments, the :math:`S` matrix defined in the expressions referenced above will be
            block-diagonal with a micro moment block equal to the covariance matrix defined in
            :eq:`averaged_micro_moment_covariances` plus any ``extra_micro_covariances``.

        micro_moments : `tuple of ProductsAgentsCovarianceMoment, optional`
            Configurations for the :math:`M_M` micro moments that will be added to the standard set of moments. The only
            type of micro moment currently supported is the :class:`ProductsAgentsCovarianceMoment`. By default, no
            micro moments are used, so :math:`M_M = 0`.

            If micro moments are specified, the micro moment block in ``W`` should usually be replaced by a matrix that
            better reflects micro moment covariances and the size of the micro dataset relative to :math:`N`. If micro
            moments were computed with substantial sampling error, ``extra_micro_covariances`` can be specified to
            account for this additional source of error.

        extra_micro_covariances : `array-like, optional`
            Covariance matrix that is added on to the :math:`M_M \times M_M` matrix of micro moments covariances defined
            in :eq:`averaged_micro_moment_covariances`, which is used to update the weighting matrix and compute
            standard errors. By default, this matrix is assumed to be zero. It should be specified if, for example,
            micro moments were computed with substantial sampling error.

        Returns
        -------
        `ProblemResults`
            :class:`ProblemResults` of the solved problem.

        Examples
        --------
            - :doc:`Tutorial </tutorial>`

        """

        # keep track of how long it takes to solve the problem
        output("Solving the problem ...")
        step_start_time = time.time()

        # validate the estimation method
        if method not in {'1s', '2s'}:
            raise TypeError("method must be '1s' or '2s'.")

        # configure or validate configurations
        if optimization is None:
            optimization = Optimization('l-bfgs-b')
        if iteration is None:
            iteration = Iteration('squarem', {'atol': 1e-14})
        if not isinstance(optimization, Optimization):
            raise TypeError("optimization must be None or an Optimization instance.")
        if not isinstance(iteration, Iteration):
            raise TypeError("iteration must be None or an Iteration instance.")

        # validate behaviors and types
        if check_optimality not in {'gradient', 'both'}:
            raise ValueError("check_optimality must be 'gradient' or 'both'.")
        if error_behavior not in {'revert', 'punish', 'raise'}:
            raise ValueError("error_behavior must be 'revert', 'punish', or 'raise'.")
        if delta_behavior not in {'last', 'first'}:
            raise ValueError("delta_behavior must be 'last' or 'first'.")
        if fp_type not in {'safe_linear', 'linear', 'safe_nonlinear', 'nonlinear'}:
            raise ValueError("fp_type must be 'safe_linear', 'linear', 'safe_nonlinear', or 'nonlinear'.")
        if costs_type not in {'linear', 'log'}:
            raise ValueError("costs_type must be 'linear' or 'log'.")
        if W_type not in {'robust', 'unadjusted', 'clustered'}:
            raise ValueError("W_type must be 'robust', 'unadjusted', or 'clustered'.")
        if se_type not in {'robust', 'unadjusted', 'clustered'}:
            raise ValueError("se_type must be 'robust', 'unadjusted', or 'clustered'.")
        if 'clustered' in {W_type, se_type} and 'clustering_ids' not in self.products.dtype.names:
            raise ValueError("W_type or se_type is 'clustered' but clustering_ids were not specified in product_data.")

        # configure or validate costs bounds
        if costs_bounds is None:
            costs_bounds = (-np.inf, +np.inf)
        else:
            if len(costs_bounds) != 2:
                raise ValueError("costs_bounds must be a tuple of the form (lb, ub).")
            costs_bounds = (np.asarray(costs_bounds[0], options.dtype), np.asarray(costs_bounds[1], options.dtype))
            costs_bounds[0][np.isnan(costs_bounds[0])] = -np.inf
            costs_bounds[1][np.isnan(costs_bounds[1])] = +np.inf
            if costs_bounds[0].size != 1:
                raise ValueError(f"The lower bound in costs_bounds must be None or a float.")
            if costs_bounds[1].size != 1:
                raise ValueError(f"The upper bound in costs_bounds must be None or a float.")
            if costs_bounds[0] > costs_bounds[1]:
                raise ValueError("The lower bound in costs_bounds cannot be larger than the upper bound.")

        # validate and structure micro moments before outputting related information
        moments = EconomyMoments(self, micro_moments)
        if moments.MM > 0:
            output("")
            output(moments.format("Micro Moments"))
            if extra_micro_covariances is not None:
                extra_micro_covariances = np.c_[np.asarray(extra_micro_covariances, options.dtype)]
                if extra_micro_covariances.shape != (moments.MM, moments.MM):
                    raise ValueError(f"extra_micro_moments must be a square {moments.MM} by {moments.MM} matrix.")
                self._detect_psd(extra_micro_covariances, "extra_micro_moments")

        # validate parameters before compressing unfixed parameters into theta and outputting related information
        parameters = Parameters(
            self, sigma, pi, rho, beta, gamma, sigma_bounds, pi_bounds, rho_bounds, beta_bounds, gamma_bounds,
            bounded=optimization._supports_bounds, allow_linear_nans=True
        )
        theta = parameters.compress()
        theta_bounds = parameters.compress_bounds()
        if parameters.fixed or parameters.unfixed:
            output("")
            output(parameters.format("Initial Values"))
            if parameters.fixed or optimization._supports_bounds:
                output("")
                output(parameters.format_lower_bounds("Lower Bounds"))
                output("")
                output(parameters.format_upper_bounds("Upper Bounds"))
                output("")

        # compute or load the weighting matrix
        if W is None:
            W, successful = precisely_invert(scipy.linalg.block_diag(
                self.products.ZD.T @ self.products.ZD,
                self.products.ZS.T @ self.products.ZS,
            ))
            if not successful:
                raise ValueError("Failed to compute the 2SLS weighting matrix. There may be instrument collinearity.")
            if moments.MM > 0:
                W = scipy.linalg.block_diag(W, np.eye(moments.MM, dtype=options.dtype))
        else:
            W = np.c_[np.asarray(W, options.dtype)]
            M = self.MD + self.MS + moments.MM
            if W.shape != (M, M):
                raise ValueError(f"W must be a square {M} by {M} matrix.")
            self._detect_psd(W, "W")

        # compute or load initial delta values
        if delta is None:
            delta = self._compute_logit_delta(parameters.rho)
        else:
            delta = np.c_[np.asarray(delta, options.dtype)]
            if delta.shape != (self.N, 1):
                raise ValueError(f"delta must be a vector with {self.N} elements.")

        # initialize marginal costs as prices, which will only be used if there are computation errors during the first
        #   objective evaluation
        tilde_costs = np.full((self.N, 0), np.nan, options.dtype)
        if self.K3 > 0:
            if costs_type == 'linear':
                tilde_costs = self.products.prices
            else:
                assert costs_type == 'log'
                tilde_costs = np.log(self.products.prices)

        # initialize micro moments as all zeros, which will only be used if there are computation errors during the
        #   first objective evaluation
        micro = np.zeros((moments.MM, 1), options.dtype)

        # initialize Jacobians as all zeros, which will only be used if there are computation errors during the first
        #   objective evaluation
        xi_jacobian = np.zeros((self.N, parameters.P), options.dtype)
        omega_jacobian = np.full((self.N, parameters.P), 0 if self.K3 > 0 else np.nan, options.dtype)
        micro_jacobian = np.zeros((moments.MM, parameters.P), options.dtype)

        # initialize the objective as a large number and its gradient and hessian as all zeros, which will only be used
        #   if there are computation errors during the first objective evaluation
        objective = np.array(1e10, options.dtype)
        gradient = np.zeros((parameters.P, 1), options.dtype)
        hessian = np.zeros((parameters.P, parameters.P), options.dtype)

        # iterate over each GMM step
        step = 1
        last_results = None
        while True:
            # collect inputs into linear parameter estimation
            X_list = [self.products.X1[:, parameters.eliminated_beta_index.flat]]
            Z_list = [self.products.ZD]
            if self.K3 > 0:
                X_list.append(self.products.X3[:, parameters.eliminated_gamma_index.flat])
                Z_list.append(self.products.ZS)

            # initialize an IV model for linear parameter estimation
            iv = IV(X_list, Z_list, W[:self.MD + self.MS, :self.MD + self.MS])
            self._handle_errors(error_behavior, iv.errors)

            # wrap computation of progress information with step-specific information
            compute_step_progress = functools.partial(
                self._compute_progress, parameters, moments, iv, W, error_behavior, error_punishment, delta_behavior,
                iteration, fp_type, costs_type, costs_bounds
            )

            # initialize optimization progress
            iteration_stats: List[Dict[Hashable, SolverStats]] = []
            smallest_objective = np.inf
            progress = InitialProgress(
                self, parameters, moments, W, theta, objective, gradient, hessian, delta, delta, tilde_costs, micro,
                xi_jacobian, omega_jacobian, micro_jacobian
            )

            # define the objective function
            def wrapper(new_theta: Array, iterations: int, evaluations: int) -> ObjectiveResults:
                """Compute and output progress associated with a single objective evaluation."""
                nonlocal iteration_stats, smallest_objective, progress
                assert optimization is not None and costs_bounds is not None
                progress = compute_step_progress(
                    new_theta, progress, optimization._compute_gradient, compute_hessian=False,
                    compute_micro_covariances=False
                )
                iteration_stats.append(progress.iteration_stats)
                formatted_progress = progress.format(
                    optimization, costs_bounds, step, iterations, evaluations, smallest_objective
                )
                if formatted_progress:
                    output(formatted_progress)
                smallest_objective = min(smallest_objective, progress.objective)
                return progress.objective, progress.gradient if optimization._compute_gradient else None

            # optimize theta
            optimization_stats = SolverStats()
            optimization_start_time = optimization_end_time = time.time()
            if parameters.P > 0:
                output(f"Starting optimization ...")
                output("")
                theta, optimization_stats = optimization._optimize(theta, theta_bounds, wrapper)
                status = "completed" if optimization_stats.converged else "failed"
                optimization_end_time = time.time()
                optimization_time = optimization_end_time - optimization_start_time
                if not optimization_stats.converged:
                    self._handle_errors(error_behavior, [exceptions.ThetaConvergenceError()])
                output("")
                output(f"Optimization {status} after {format_seconds(optimization_time)}.")

            # identify what will be done when computing results
            last_step = method != '2s' or step == 2
            compute_gradient = parameters.P > 0
            compute_hessian = compute_gradient and check_optimality == 'both'
            compute_micro_covariances = moments.MM > 0

            # use progress information computed at the optimal theta to compute results for the step
            if compute_hessian and not last_step:
                output("Computing the Hessian and and updating the weighting matrix ...")
            elif compute_hessian:
                output("Computing the Hessian and estimating standard errors ...")
            elif not last_step:
                output("Updating the weighting matrix ...")
            else:
                output("Estimating standard errors ...")
            final_progress = compute_step_progress(
                theta, progress, compute_gradient, compute_hessian, compute_micro_covariances
            )
            optimization_stats.evaluations += 1
            results = ProblemResults(
                final_progress, last_results, last_step, step_start_time, optimization_start_time,
                optimization_end_time, optimization_stats, iteration_stats, costs_type, costs_bounds,
                extra_micro_covariances, center_moments, W_type, se_type
            )
            self._handle_errors(error_behavior, results._errors)
            output(f"Computed results after {format_seconds(results.total_time - results.optimization_time)}.")

            # store the last results and return results from the final step
            last_results = results
            output("")
            if not last_step:
                output(results._format_summary())
                output("")
            else:
                output(results)
                return results

            # update vectors and matrices
            delta = results.delta
            tilde_costs = results.tilde_costs
            xi_jacobian = results.xi_by_theta_jacobian
            omega_jacobian = results.omega_by_theta_jacobian
            W = results.updated_W
            step += 1
            step_start_time = time.time()

    def _compute_progress(
            self, parameters: Parameters, moments: EconomyMoments, iv: IV, W: Array, error_behavior: str,
            error_punishment: float, delta_behavior: str, iteration: Iteration, fp_type: str, costs_type: str,
            costs_bounds: Bounds, theta: Array, progress: 'InitialProgress', compute_gradient: bool,
            compute_hessian: bool, compute_micro_covariances: bool) -> 'Progress':
        """Compute demand- and supply-side contributions before recovering the linear parameters and structural error
        terms. Then, form the GMM objective value and its gradient. Finally, handle any errors that were encountered
        before structuring relevant progress information.
        """
        errors: List[Error] = []

        # expand theta
        sigma, pi, rho, beta, gamma = parameters.expand(theta)

        # compute demand-side contributions
        delta, micro, xi_jacobian, micro_jacobian, micro_covariances, iteration_stats, demand_errors = (
            self._compute_demand_contributions(
                parameters, moments, iteration, fp_type, sigma, pi, rho, progress, compute_gradient,
                compute_micro_covariances
            )
        )
        errors.extend(demand_errors)

        # compute supply-side contributions
        if self.K3 == 0:
            tilde_costs = np.full((self.N, 0), np.nan, options.dtype)
            omega_jacobian = np.full((self.N, parameters.P), np.nan, options.dtype)
            clipped_costs = np.zeros((self.N, 1), np.bool)
        else:
            supply = self._compute_supply_contributions(
                parameters, costs_type, costs_bounds, sigma, pi, rho, beta, delta, xi_jacobian, progress,
                compute_gradient
            )
            tilde_costs, omega_jacobian, clipped_costs, supply_errors = supply
            errors.extend(supply_errors)

        # subtract contributions of linear parameters in theta
        iv_delta = delta.copy()
        iv_tilde_costs = tilde_costs.copy()
        if not parameters.eliminated_beta_index.all():
            theta_beta = np.c_[beta[~parameters.eliminated_beta_index]]
            iv_delta -= self._compute_true_X1(index=~parameters.eliminated_beta_index.flatten()) @ theta_beta
        if not parameters.eliminated_gamma_index.all():
            theta_gamma = np.c_[gamma[~parameters.eliminated_gamma_index]]
            iv_delta -= self._compute_true_X3(index=~parameters.eliminated_gamma_index.flatten()) @ theta_gamma

        # absorb any fixed effects
        if self._absorb_demand_ids is not None:
            iv_delta, demand_absorption_errors = self._absorb_demand_ids(iv_delta)
            errors.extend(demand_absorption_errors)
        if self._absorb_supply_ids is not None:
            iv_tilde_costs, supply_absorption_errors = self._absorb_supply_ids(iv_tilde_costs)
            errors.extend(supply_absorption_errors)

        # collect inputs into GMM estimation
        X_list = [self.products.X1[:, parameters.eliminated_beta_index.flat]]
        Z_list = [self.products.ZD]
        y_list = [iv_delta]
        jacobian_list = [xi_jacobian]
        if self.K3 > 0:
            X_list.append(self.products.X3[:, parameters.eliminated_gamma_index.flat])
            Z_list.append(self.products.ZS)
            y_list.append(iv_tilde_costs)
            jacobian_list.append(omega_jacobian)

        # recover the linear parameters and structural error terms
        parameters_list, u_list = iv.estimate(X_list, Z_list, W[:self.MD + self.MS, :self.MD + self.MS], y_list)
        beta[parameters.eliminated_beta_index] = parameters_list[0].flat
        xi = u_list[0]
        if self.K3 == 0:
            omega = np.full((self.N, 0), np.nan, options.dtype)
        else:
            gamma[parameters.eliminated_gamma_index] = parameters_list[1].flat
            omega = u_list[1]

        # compute the objective value and replace it with its last value if computation failed
        with np.errstate(all='ignore'):
            mean_g = np.r_[compute_gmm_moments_mean(u_list, Z_list), micro]
            objective = self.N**2 * mean_g.T @ W @ mean_g
        if not np.isfinite(np.squeeze(objective)):
            objective = progress.objective
            errors.append(exceptions.ObjectiveReversionError())

        # compute the gradient and replace any invalid elements with their last values
        gradient = np.full_like(progress.gradient, np.nan)
        if compute_gradient:
            with np.errstate(all='ignore'):
                mean_G = np.r_[compute_gmm_moments_jacobian_mean(jacobian_list, Z_list), micro_jacobian]
                gradient = 2 * self.N**2 * (mean_G.T @ W @ mean_g)
            bad_gradient_index = ~np.isfinite(gradient)
            if np.any(bad_gradient_index):
                gradient[bad_gradient_index] = progress.gradient[bad_gradient_index]
                errors.append(exceptions.GradientReversionError(bad_gradient_index))

        # handle any errors
        if errors:
            if error_behavior == 'raise':
                raise exceptions.MultipleErrors(errors)
            if error_behavior == 'revert':
                objective *= error_punishment
            else:
                assert error_behavior == 'punish'
                objective = np.array(error_punishment)
                if compute_gradient:
                    gradient = np.zeros_like(progress.gradient)

        # select the delta that will be used in the next objective evaluation
        if delta_behavior == 'last':
            next_delta = delta
        else:
            assert delta_behavior == 'first'
            next_delta = progress.next_delta

        # compute the hessian with central finite differences
        hessian = np.full_like(progress.hessian, np.nan)
        if compute_hessian:
            compute_progress = lambda x: self._compute_progress(
                parameters, moments, iv, W, error_behavior, error_punishment, delta_behavior, iteration, fp_type,
                costs_type, costs_bounds, x, progress, compute_gradient=True, compute_hessian=False,
                compute_micro_covariances=False
            )
            change = np.sqrt(np.finfo(np.float64).eps)
            for p in range(parameters.P):
                theta1 = theta.copy()
                theta2 = theta.copy()
                theta1[p] += change / 2
                theta2[p] -= change / 2
                hessian[:, [p]] = (compute_progress(theta1).gradient - compute_progress(theta2).gradient) / change

            # enforce shape and symmetry
            hessian = np.c_[hessian + hessian.T] / 2

        # structure progress
        return Progress(
            self, parameters, moments, W, theta, objective, gradient, hessian, next_delta, delta, tilde_costs, micro,
            xi_jacobian, omega_jacobian, micro_jacobian, micro_covariances, xi, omega, beta, gamma, iteration_stats,
            clipped_costs, errors
        )

    def _compute_demand_contributions(
            self, parameters: Parameters, moments: EconomyMoments, iteration: Iteration, fp_type: str, sigma: Array,
            pi: Array, rho: Array, progress: 'InitialProgress', compute_jacobian: bool,
            compute_micro_covariances: bool) -> (
            Tuple[Array, Array, Array, Array, Array, Dict[Hashable, SolverStats], List[Error]]):
        """Compute delta and the Jacobian of xi (equivalently, of delta) with respect to theta market-by-market. If
        there are any micro moments, compute them (taking the average across relevant markets) along with their
        Jacobian and covariances. Revert any problematic elements to their last values.
        """
        errors: List[Error] = []

        # initialize delta, micro moments, their Jacobians, micro moment covariances, and fixed point statistics so that
        #   they can be filled
        delta = np.zeros((self.N, 1), options.dtype)
        micro = np.zeros((moments.MM, 1), options.dtype)
        xi_jacobian = np.zeros((self.N, parameters.P), options.dtype)
        micro_jacobian = np.zeros((moments.MM, parameters.P), options.dtype)
        micro_covariances = np.zeros((moments.MM, moments.MM), options.dtype)
        iteration_stats: Dict[Hashable, SolverStats] = {}

        # when possible and when a gradient isn't needed, compute delta with a closed-form solution
        if self.K2 == 0 and moments.MM == 0 and (parameters.P == 0 or not compute_jacobian):
            delta = self._compute_logit_delta(rho)
        else:
            # define a factory for solving the demand side of problem markets
            def market_factory(s: Hashable) -> Tuple[ProblemMarket, Array, Iteration, str, bool, bool]:
                """Build a market along with arguments used to compute delta, micro moment values, and Jacobians."""
                market_s = ProblemMarket(self, s, parameters, sigma, pi, rho, moments=moments)
                initial_delta_s = progress.next_delta[self._product_market_indices[s]]
                return market_s, initial_delta_s, iteration, fp_type, compute_jacobian, compute_micro_covariances

            # compute delta, micro moments, their Jacobians, and micro moment covariances market-by-market
            micro_mapping: Dict[Hashable, Array] = {}
            micro_jacobian_mapping: Dict[Hashable, Array] = {}
            micro_covariances_mapping: Dict[Hashable, Array] = {}
            generator = generate_items(self.unique_market_ids, market_factory, ProblemMarket.solve_demand)
            for t, (delta_t, micro_t, xi_jacobian_t, micro_jacobian_t, covariances_t, stats_t, errors_t) in generator:
                delta[self._product_market_indices[t]] = delta_t
                xi_jacobian[self._product_market_indices[t], :parameters.P] = xi_jacobian_t
                micro_mapping[t] = micro_t
                micro_jacobian_mapping[t] = micro_jacobian_t
                micro_covariances_mapping[t] = covariances_t
                iteration_stats[t] = stats_t
                errors.extend(errors_t)

            # average micro moments, their Jacobian, and their covariances across all markets (this is done after
            #   market-by-market computation to preserve numerical stability with different market orderings)
            if moments.MM > 0:
                with np.errstate(all='ignore'):
                    for t in self.unique_market_ids:
                        indices = moments.market_indices[t]
                        micro[indices] += micro_mapping[t] / moments.market_counts[indices]
                        micro_jacobian[indices, :parameters.P] += (
                            micro_jacobian_mapping[t] / moments.market_counts[indices]
                        )
                        if compute_micro_covariances:
                            pairwise_indices = tuple(np.meshgrid(indices, indices))
                            micro_covariances[pairwise_indices] += (
                                micro_covariances_mapping[t] / moments.pairwise_market_counts[pairwise_indices]
                            )

        # replace invalid elements in delta and the micro moment values with their last values
        bad_delta_index = ~np.isfinite(delta)
        bad_micro_index = ~np.isfinite(micro)
        if np.any(bad_delta_index):
            delta[bad_delta_index] = progress.delta[bad_delta_index]
            errors.append(exceptions.DeltaReversionError(bad_delta_index))
        if np.any(bad_micro_index):
            micro[bad_micro_index] = progress.micro[bad_micro_index]
            errors.append(exceptions.MicroMomentsReversionError(bad_micro_index))

        # replace invalid elements in the Jacobians with their last values
        if compute_jacobian:
            bad_xi_jacobian_index = ~np.isfinite(xi_jacobian)
            bad_micro_jacobian_index = ~np.isfinite(micro_jacobian)
            if np.any(bad_xi_jacobian_index):
                xi_jacobian[bad_xi_jacobian_index] = progress.xi_jacobian[bad_xi_jacobian_index]
                errors.append(exceptions.XiByThetaJacobianReversionError(bad_xi_jacobian_index))
            if np.any(bad_micro_jacobian_index):
                micro_jacobian[bad_micro_jacobian_index] = progress.micro_jacobian[bad_micro_jacobian_index]
                errors.append(exceptions.MicroMomentsByThetaJacobianReversionError(bad_micro_jacobian_index))
        return delta, micro, xi_jacobian, micro_jacobian, micro_covariances, iteration_stats, errors

    def _compute_supply_contributions(
            self, parameters: Parameters, costs_type: str, costs_bounds: Bounds, sigma: Array, pi: Array, rho: Array,
            beta: Array, delta: Array, xi_jacobian: Array, progress: 'InitialProgress', compute_jacobian: bool) -> (
            Tuple[Array, Array, Array, List[Error]]):
        """Compute transformed marginal costs and the Jacobian of omega (equivalently, of transformed marginal costs)
        with respect to theta market-by-market. Revert any problematic elements to their last values.
        """
        errors: List[Error] = []

        # initialize transformed marginal costs, their Jacobian, and indices of clipped costs so that they can be filled
        tilde_costs = np.zeros((self.N, 1), options.dtype)
        omega_jacobian = np.zeros((self.N, parameters.P), options.dtype)
        clipped_costs = np.zeros((self.N, 1), np.bool)

        # define a factory for solving the supply side of problem markets
        def market_factory(
                s: Hashable) -> Tuple[ProblemMarket, Array, Array, str, Bounds, bool]:
            """Build a market along with arguments used to compute transformed marginal costs and their Jacobian."""
            market_s = ProblemMarket(self, s, parameters, sigma, pi, rho, beta, delta)
            last_tilde_costs_s = progress.tilde_costs[self._product_market_indices[s]]
            xi_jacobian_s = xi_jacobian[self._product_market_indices[s]]
            return market_s, last_tilde_costs_s, xi_jacobian_s, costs_type, costs_bounds, compute_jacobian

        # compute transformed marginal costs and their Jacobian market-by-market
        generator = generate_items(self.unique_market_ids, market_factory, ProblemMarket.solve_supply)
        for t, (tilde_costs_t, omega_jacobian_t, clipped_costs_t, errors_t) in generator:
            tilde_costs[self._product_market_indices[t]] = tilde_costs_t
            omega_jacobian[self._product_market_indices[t], :parameters.P] = omega_jacobian_t
            clipped_costs[self._product_market_indices[t]] = clipped_costs_t
            errors.extend(errors_t)

        # replace invalid transformed marginal costs with their last values
        bad_tilde_costs_index = ~np.isfinite(tilde_costs)
        if np.any(bad_tilde_costs_index):
            tilde_costs[bad_tilde_costs_index] = progress.tilde_costs[bad_tilde_costs_index]
            errors.append(exceptions.CostsReversionError(bad_tilde_costs_index))

        # replace invalid elements in their Jacobian with their last values
        if compute_jacobian:
            bad_omega_jacobian_index = ~np.isfinite(omega_jacobian)
            if np.any(bad_omega_jacobian_index):
                omega_jacobian[bad_omega_jacobian_index] = progress.omega_jacobian[bad_omega_jacobian_index]
                errors.append(exceptions.OmegaByThetaJacobianReversionError(bad_omega_jacobian_index))
        return tilde_costs, omega_jacobian, clipped_costs, errors

    def _compute_logit_delta(self, rho: Array) -> Array:
        """Compute the delta that solves the simple logit (or nested logit) model."""
        log_shares = np.log(self.products.shares)
        delta = log_shares.copy()
        for t in self.unique_market_ids:
            shares_t = self.products.shares[self._product_market_indices[t]]
            log_outside_share_t = np.log(1 - shares_t.sum())
            delta[self._product_market_indices[t]] -= log_outside_share_t
            if self.H > 0:
                log_shares_t = log_shares[self._product_market_indices[t]]
                groups_t = Groups(self.products.nesting_ids[self._product_market_indices[t]])
                log_group_shares_t = np.log(groups_t.expand(groups_t.sum(shares_t)))
                if rho.size == 1:
                    rho_t = np.full_like(shares_t, float(rho))
                else:
                    rho_t = groups_t.expand(rho[np.searchsorted(self.unique_nesting_ids, groups_t.unique)])
                delta[self._product_market_indices[t]] -= rho_t * (log_shares_t - log_group_shares_t)
        return delta

    @staticmethod
    def _handle_errors(error_behavior: str, errors: List[Error]) -> None:
        """Either raise or output information about any errors."""
        if errors:
            if error_behavior == 'raise':
                raise exceptions.MultipleErrors(errors)
            output("")
            output(exceptions.MultipleErrors(errors))
            output("")


class Problem(ProblemEconomy):
    r"""A BLP-type problem.

    This class is initialized with relevant data and solved with :meth:`Problem.solve`.

    Parameters
    ----------
    product_formulations : `Formulation or tuple of Formulation`
        :class:`Formulation` configuration or tuple of up to three :class:`Formulation` configurations for the matrix
        of linear product characteristics, :math:`X_1`, for the matrix of nonlinear product characteristics,
        :math:`X_2`, and for the matrix of cost characteristics, :math:`X_3`, respectively. If the formulation for
        :math:`X_3` is not specified or is ``None``, a supply side will not be estimated. Similarly, if the formulation
        for :math:`X_2` is not specified or is ``None``, the logit (or nested logit) model will be estimated.

        Variable names should correspond to fields in ``product_data``. The ``shares`` variable should not be included
        in any of the formulations and ``prices`` should be included in the formulation for :math:`X_1` or :math:`X_2`
        (or both). The ``absorb`` argument of :class:`Formulation` can be used to absorb fixed effects into :math:`X_1`
        and :math:`X_3`, but not :math:`X_2`. Characteristics in :math:`X_2` should generally be included in
        :math:`X_1`. The typical exception is characteristics that are collinear with fixed effects that have been
        absorbed into :math:`X_1`.

        Characteristics in :math:`X_1` that do not involve ``prices``, :math:`X_1^x`, will be combined with excluded
        demand-side instruments (specified below) to create the full set of demand-side instruments, :math:`Z_D`. Any
        fixed effects absorbed into :math:`X_1` will also be absorbed into :math:`Z_D`. Similarly, characteristics in
        :math:`X_3` will be combined with the excluded supply-side instruments to create :math:`Z_S`, and any fixed
        effects absorbed into :math:`X_3` will also be absorbed into :math:`Z_S`.

        .. warning::

           Characteristics that involve prices, :math:`p`, should always be formulated with the ``prices`` variable. If
           another name is used, :class:`Problem` will not understand that the characteristic is endogenous, so it will
           be erroneously included in :math:`Z_D`, and derivatives computed with respect to prices will likely be wrong.
           For example, to include a :math:`p^2` characteristic, include ``I(prices**2)`` in a formula instead of
           manually including a ``prices_squared`` variable in ``product_data`` and a formula.

    product_data : `structured array-like`
        Each row corresponds to a product. Markets can have differing numbers of products. The following fields are
        required:

            - **market_ids** : (`object`) - IDs that associate products with markets.

            - **shares** : (`numeric`) - Marketshares, :math:`s`, which should be between zero and one, exclusive.
              Outside shares should also be between zero and one. Shares in each market should sum to less than one.

            - **prices** : (`numeric`) - Product prices, :math:`p`.

        If a formulation for :math:`X_3` is specified in ``product_formulations``, firm IDs are also required, since
        they will be used to estimate the supply side of the problem:

            - **firm_ids** : (`object, optional`) - IDs that associate products with firms.

        Excluded instruments should generally be specified with the following fields:

            - **demand_instruments** : (`numeric`) - Excluded demand-side instruments, which, together with the
              formulated exogenous linear product characteristics, :math:`X_1^x`, constitute the full set of demand-side
              instruments, :math:`Z_D`.

            - **supply_instruments** : (`numeric, optional`) - Excluded supply-side instruments, which, together with
              the formulated cost characteristics, :math:`X_3`, constitute the full set of supply-side instruments,
              :math:`Z_S`.

        The recommendation in :ref:`references:Conlon and Gortmaker (2019)` is to start with differentiation instruments
        of :ref:`references:Gandhi and Houde (2017)`, which can be built with :func:`build_differentiation_instruments`,
        and then compute feasible optimal instruments with :func:`ProblemResults.compute_optimal_instruments` in the
        second stage.

        If ``firm_ids`` are specified, custom ownership matrices can be specified as well:

            - **ownership** : (`numeric, optional`) - Custom stacked :math:`J_t \times J_t` ownership matrices,
              :math:`O`, for each market :math:`t`, which can be built with :func:`build_ownership`. By default,
              standard ownership matrices are built only when they are needed to reduce memory usage. If specified,
              there should be as many columns as there are products in the market with the most products. Rightmost
              columns in markets with fewer products will be ignored.

        .. note::

           Fields that can have multiple columns (``demand_instruments``, ``supply_instruments``, and ``ownership``) can
           either be matrices or can be broken up into multiple one-dimensional fields with column index suffixes that
           start at zero. For example, if there are three columns of excluded demand-side instruments, a
           ``demand_instruments`` field with three columns can be replaced by three one-dimensional fields:
           ``demand_instruments0``, ``demand_instruments1``, and ``demand_instruments2``.

        To estimate a nested logit or random coefficients nested logit (RCNL) model, nesting groups must be specified:

            - **nesting_ids** (`object, optional`) - IDs that associate products with nesting groups. When these IDs are
              specified, ``rho`` must be specified in :meth:`Problem.solve` as well.

        Finally, clustering groups can be specified to account for within-group correlation while updating the weighting
        matrix and estimating standard errors:

            - **clustering_ids** (`object, optional`) - Cluster group IDs, which will be used if ``W_type`` or
              ``se_type`` in :meth:`Problem.solve` is ``'clustered'``.

        Along with ``market_ids``, ``firm_ids``, ``nesting_ids``, ``clustering_ids``, and ``prices``, the names of any
        additional fields can typically be used as variables in ``product_formulations``. However, there are a few
        variable names such as ``'X1'``, which are reserved for use by :class:`Products`.

    agent_formulation : `Formulation, optional`
        :class:`Formulation` configuration for the matrix of observed agent characteristics called demographics,
        :math:`d`, which will only be included in the model if this formulation is specified. Since demographics are
        only used if there are nonlinear product characteristics, this formulation should only be specified if
        :math:`X_2` is formulated in ``product_formulations``. Variable names should correspond to fields in
        ``agent_data``.
    agent_data : `structured array-like, optional`
        Each row corresponds to an agent. Markets can have differing numbers of agents. Since simulated agents are only
        used if there are nonlinear product characteristics, agent data should only be specified if :math:`X_2` is
        formulated in ``product_formulations``. If agent data are specified, market IDs are required:

            - **market_ids** : (`object`) - IDs that associate agents with markets. The set of distinct IDs should be
              the same as the set in ``product_data``. If ``integration`` is specified, there must be at least as many
              rows in each market as the number of nodes and weights that are built for the market.

        If ``integration`` is not specified, the following fields are required:

            - **weights** : (`numeric, optional`) - Integration weights, :math:`w`, for integration over agent choice
              probabilities.

            - **nodes** : (`numeric, optional`) - Unobserved agent characteristics called integration nodes,
              :math:`\nu`. If there are more than :math:`K_2` columns (the number of nonlinear product characteristics),
              only the first :math:`K_2` will be retained.

        The convenience function :func:`build_integration` can be useful when constructing custom nodes and weights.

        .. note::

           If ``nodes`` has multiple columns, it can be specified as a matrix or broken up into multiple one-dimensional
           fields with column index suffixes that start at zero. For example, if there are three columns of nodes, a
           ``nodes`` field with three columns can be replaced by three one-dimensional fields: ``nodes0``, ``nodes1``,
           and ``nodes2``.

        Along with ``market_ids``, the names of any additional fields can be typically be used as variables in
        ``agent_formulation``. The exception is the name ``'demographics'``, which is reserved for use by
        :class:`Agents`.

    integration : `Integration, optional`
        :class:`Integration` configuration for how to build nodes and weights for integration over agent choice
        probabilities, which will replace any ``nodes`` and ``weights`` fields in ``agent_data``. This configuration is
        required if ``nodes`` and ``weights`` in ``agent_data`` are not specified. It should not be specified if
        :math:`X_2` is not formulated in ``product_formulations``.

        If this configuration is specified, :math:`K_2` columns of nodes (the number of nonlinear product
        characteristics) will be built. However, if ``sigma`` in :meth:`Problem.solve` is left unspecified or
        specified with columns fixed at zero, fewer columns will be used.

    Attributes
    ----------
    product_formulations : `Formulation or tuple of Formulation`
        :class:`Formulation` configurations for :math:`X_1`, :math:`X_2`, and :math:`X_3`, respectively.
    agent_formulation : `Formulation`
        :class:`Formulation` configuration for :math:`d`.
    products : `Products`
        Product data structured as :class:`Products`, which consists of data taken from ``product_data`` along with
        matrices built according to :attr:`Problem.product_formulations`.
    agents : `Agents`
        Agent data structured as :class:`Agents`, which consists of data taken from ``agent_data`` or built by
        ``integration`` along with any demographics built according to :attr:`Problem.agent_formulation`.
    unique_market_ids : `ndarray`
        Unique market IDs in product and agent data.
    unique_firm_ids : `ndarray`
        Unique firm IDs in product data.
    unique_nesting_ids : `ndarray`
        Unique nesting group IDs in product data.
    T : `int`
        Number of markets, :math:`T`.
    N : `int`
        Number of products across all markets, :math:`N`.
    F : `int`
        Number of firms across all markets, :math:`F`.
    I : `int`
        Number of agents across all markets, :math:`I`.
    K1 : `int`
        Number of linear product characteristics, :math:`K_1`.
    K2 : `int`
        Number of nonlinear product characteristics, :math:`K_2`.
    K3 : `int`
        Number of cost product characteristics, :math:`K_3`.
    D : `int`
        Number of demographic variables, :math:`D`.
    MD : `int`
        Number of demand-side instruments, :math:`M_D`, which is the number of excluded demand-side instruments plus
        the number of exogenous linear product characteristics, :math:`K_1^x`.
    MS : `int`
        Number of supply-side instruments, :math:`M_S`, which is the number of excluded supply-side instruments plus
        the number of cost product characteristics, :math:`K_3`.
    ED : `int`
        Number of absorbed dimensions of demand-side fixed effects, :math:`E_D`.
    ES : `int`
        Number of absorbed dimensions of supply-side fixed effects, :math:`E_S`.
    H : `int`
        Number of nesting groups, :math:`H`.

    Examples
    --------
        - :doc:`Tutorial </tutorial>`

    """

    def __init__(
            self, product_formulations: Union[Formulation, Sequence[Optional[Formulation]]], product_data: Mapping,
            agent_formulation: Optional[Formulation] = None, agent_data: Optional[Mapping] = None,
            integration: Optional[Integration] = None) -> None:
        """Initialize the underlying economy with product and agent data before absorbing fixed effects."""

        # keep track of long it takes to initialize the problem
        output("Initializing the problem ...")
        start_time = time.time()

        # validate and normalize product formulations
        if isinstance(product_formulations, Formulation):
            product_formulations = [product_formulations]
        elif isinstance(product_formulations, collections.Sequence) and len(product_formulations) <= 3:
            product_formulations = list(product_formulations)
        else:
            raise TypeError("product_formulations must be a Formulation instance or a tuple of up to three instances.")
        product_formulations.extend([None] * (3 - len(product_formulations)))

        # initialize the underlying economy with structured product and agent data
        products = Products(product_formulations, product_data)
        agents = Agents(products, agent_formulation, agent_data, integration)
        super().__init__(product_formulations, agent_formulation, products, agents)

        # absorb any demand-side fixed effects
        if self._absorb_demand_ids is not None:
            output("Absorbing demand-side fixed effects ...")
            self.products.X1, X1_errors = self._absorb_demand_ids(self.products.X1)
            self.products.ZD, ZD_errors = self._absorb_demand_ids(self.products.ZD)
            if X1_errors or ZD_errors:
                raise exceptions.MultipleErrors(X1_errors + ZD_errors)

        # absorb any supply-side fixed effects
        if self._absorb_supply_ids is not None:
            output("Absorbing supply-side fixed effects ...")
            self.products.X3, X3_errors = self._absorb_supply_ids(self.products.X3)
            self.products.ZS, ZS_errors = self._absorb_supply_ids(self.products.ZS)
            if X3_errors or ZS_errors:
                raise exceptions.MultipleErrors(X3_errors + ZS_errors)

        # detect any problems with the product data
        self._validate_shares()
        self._detect_collinearity()

        # output information about the initialized problem
        output(f"Initialized the problem after {format_seconds(time.time() - start_time)}.")
        output("")
        output(self)


class OptimalInstrumentProblem(ProblemEconomy):
    """A BLP problem updated with optimal excluded instruments.

    This class can be used exactly like :class:`Problem`.

    """

    def __init__(self, problem: ProblemEconomy, demand_instruments: Array, supply_instruments: Array) -> None:
        """Initialize the underlying economy with updated product data before absorbing fixed effects."""

        # keep track of long it takes to re-create the problem
        output("Re-creating the problem ...")
        start_time = time.time()

        # supplement the excluded demand-side instruments with exogenous characteristics in X1
        X1 = problem._compute_true_X1()
        ZD = demand_instruments
        for index, formulation in enumerate(problem._X1_formulations):
            if 'prices' not in formulation.names:
                ZD = np.c_[ZD, X1[:, [index]]]

        # supplement the excluded supply-side instruments with X3
        X3 = problem._compute_true_X3()
        ZS = np.c_[supply_instruments, X3]

        # update the products array
        updated_products = update_matrices(problem.products, {
            'ZD': (ZD, options.dtype),
            'ZS': (ZS, options.dtype)
        })

        # initialize the underlying economy with structured product and agent data
        super().__init__(problem.product_formulations, problem.agent_formulation, updated_products, problem.agents)

        # absorb any demand-side fixed effects, which have already been absorbed into X1
        if self._absorb_demand_ids is not None:
            output("Absorbing demand-side fixed effects ...")
            self.products.ZD, ZD_errors = self._absorb_demand_ids(self.products.ZD)
            if ZD_errors:
                raise exceptions.MultipleErrors(ZD_errors)

        # absorb any supply-side fixed effects, which have already been absorbed into X3
        if self._absorb_supply_ids is not None:
            output("Absorbing supply-side fixed effects ...")
            self.products.ZS, ZS_errors = self._absorb_supply_ids(self.products.ZS)
            if ZS_errors:
                raise exceptions.MultipleErrors(ZS_errors)

        # detect any collinearity issues with the updated instruments
        self._detect_collinearity()

        # output information about the re-created problem
        output(f"Re-created the problem after {format_seconds(time.time() - start_time)}.")
        output("")
        output(self)


class InitialProgress(object):
    """Structured information about initial estimation progress."""

    problem: ProblemEconomy
    parameters: Parameters
    moments: EconomyMoments
    W: Array
    theta: Array
    objective: Array
    gradient: Array
    hessian: Array
    next_delta: Array
    delta: Array
    tilde_costs: Array
    micro: Array
    xi_jacobian: Array
    omega_jacobian: Array
    micro_jacobian: Array

    def __init__(
            self, problem: ProblemEconomy, parameters: Parameters, moments: EconomyMoments, W: Array, theta: Array,
            objective: Array, gradient: Array, hessian: Array, next_delta: Array, delta: Array, tilde_costs: Array,
            micro: Array, xi_jacobian: Array, omega_jacobian: Array, micro_jacobian: Array) -> None:
        """Store initial progress information, computing the projected gradient and the reduced Hessian."""
        self.problem = problem
        self.parameters = parameters
        self.moments = moments
        self.W = W
        self.theta = theta
        self.objective = objective
        self.gradient = gradient
        self.hessian = hessian
        self.next_delta = next_delta
        self.delta = delta
        self.tilde_costs = tilde_costs
        self.micro = micro
        self.xi_jacobian = xi_jacobian
        self.omega_jacobian = omega_jacobian
        self.micro_jacobian = micro_jacobian


class Progress(InitialProgress):
    """Structured information about estimation progress."""

    micro_covariances: Array
    xi: Array
    omega: Array
    beta: Array
    gamma: Array
    iteration_stats: Dict[Hashable, SolverStats]
    clipped_costs: Array
    errors: List[Error]
    projected_gradient: Array
    reduced_hessian: Array
    projected_gradient_norm: Array

    def __init__(
            self, problem: ProblemEconomy, parameters: Parameters, moments: EconomyMoments, W: Array, theta: Array,
            objective: Array, gradient: Array, hessian: Array, next_delta: Array, delta: Array, tilde_costs: Array,
            micro: Array, xi_jacobian: Array, omega_jacobian: Array, micro_jacobian: Array, micro_covariances: Array,
            xi: Array, omega: Array, beta: Array, gamma: Array, iteration_stats: Dict[Hashable, SolverStats],
            clipped_costs: Array, errors: List[Error]) -> None:
        """Store progress information, compute the projected gradient and its norm, and compute the reduced Hessian."""
        super().__init__(
            problem, parameters, moments, W, theta, objective, gradient, hessian, next_delta, delta, tilde_costs, micro,
            xi_jacobian, omega_jacobian, micro_jacobian
        )
        self.micro_covariances = micro_covariances
        self.xi = xi
        self.omega = omega
        self.beta = beta
        self.gamma = gamma
        self.iteration_stats = iteration_stats or {}
        self.clipped_costs = clipped_costs
        self.errors = errors or []

        # compute the projected gradient and the reduced Hessian
        self.projected_gradient = self.gradient.copy()
        self.reduced_hessian = self.hessian.copy()
        for p, (lb, ub) in enumerate(self.parameters.compress_bounds()):
            if not lb < theta[p] < ub:
                self.reduced_hessian[p] = self.reduced_hessian[:, p] = 0
                with np.errstate(invalid='ignore'):
                    if theta[p] <= lb:
                        self.projected_gradient[p] = min(0, self.gradient[p])
                    elif theta[p] >= ub:
                        self.projected_gradient[p] = max(0, self.gradient[p])

        # compute the norm of the projected gradient
        self.projected_gradient_norm = np.array(np.nan, options.dtype)
        if gradient.size > 0:
            with np.errstate(invalid='ignore'):
                self.projected_gradient_norm = np.abs(self.projected_gradient).max()

    def format(
            self, optimization: Optimization, costs_bounds: Bounds, step: int, iterations: int, evaluations: int,
            smallest_objective: Array) -> str:
        """Format a universal display of optimization progress as a string. The first iteration will include the
        progress table header. If there are any errors, information about them will be formatted as well, regardless of
        whether or not a universal display is to be used. The smallest_objective is the smallest objective value
        encountered so far during optimization.
        """
        lines: List[str] = []

        # include information about any errors
        if self.errors:
            preamble = (
                "At least one error was encountered. As long as the optimization routine does not get stuck at values "
                "of theta that give rise to errors, this is not necessarily a problem. If the errors persist or seem "
                "to be impacting the optimization results, consider setting an error punishment or following any of "
                "the other suggestions below:"
            )
            lines.extend(["", preamble, str(exceptions.MultipleErrors(self.errors)), ""])

        # only output errors if the solver's display is being used
        if not optimization._universal_display:
            return "\n".join(lines)

        # construct the leftmost part of the table that always shows up
        header = [
            ("GMM", "Step"), ("Optimization", "Iterations"), ("Objective", "Evaluations"),
            ("Fixed Point", "Iterations"), ("Contraction", "Evaluations")
        ]
        values = [
            str(step),
            str(iterations),
            str(evaluations),
            str(sum(s.iterations for s in self.iteration_stats.values())),
            str(sum(s.evaluations for s in self.iteration_stats.values()))
        ]

        # add a count of any clipped marginal costs
        if np.isfinite(costs_bounds).any():
            header.append(("Clipped", "Costs"))
            values.append(str(self.clipped_costs.sum()))

        # add information about the objective
        header.extend([("Objective", "Value"), ("Objective", "Improvement")])
        values.append(format_number(self.objective))
        improvement = smallest_objective - self.objective
        if np.isfinite(improvement) and improvement > 0:
            values.append(format_number(smallest_objective - self.objective))
        else:
            values.append(" " * len(format_number(improvement)))

        # add information about the gradient
        if optimization._compute_gradient:
            header.append(("Projected", "Gradient Norm") if self.parameters.any_bounds else ("Gradient", "Norm"))
            values.append(format_number(self.projected_gradient_norm))

        # add information about theta
        header.append(("", "Theta"))
        values.append(", ".join(format_number(x) for x in self.theta))

        # add information about micro moments
        if self.moments.MM > 0:
            header.append(("Micro", "Moments"))
            values.append(", ".join(format_number(x) for x in self.micro))

        # format the table
        lines.append(format_table(header, values, include_border=False, include_header=evaluations == 1))
        return "\n".join(lines)
