from dataclasses import dataclass
import jax.numpy as jnp
import numpyro.distributions as dist
from jax import vmap, random
from jax.lax import scan

from lqg.kalman import kalman_gain
from lqg.lqr import control_law


@dataclass
class Dynamics:
    A: jnp.array
    B: jnp.array
    C: jnp.array
    V: jnp.array
    W: jnp.array


@dataclass
class Actor:
    A: jnp.array
    B: jnp.array
    C: jnp.array
    V: jnp.array
    W: jnp.array
    Q: jnp.array
    R: jnp.array

    def L(self, T):
        return control_law(self.A, self.B, self.Q, self.R, T=T)

    def K(self, T):
        return kalman_gain(self.A, self.C, self.V @ self.V.T, self.W @ self.W.T, T=T)


class System:
    def __init__(self, actor, dynamics):
        self.actor = actor
        self.dynamics = dynamics

    @property
    def xdim(self):
        """ State dimensionality

        Returns:
            int: dimensionality of state
        """
        return self.dynamics.A.shape[0]

    @property
    def bdim(self):
        """ Belief dimensionality

        Returns:
            int: dimensionality of belief
        """
        return self.actor.A.shape[0]

    @property
    def udim(self):
        """ Action dimensionality

        Returns:
            int: dimensionality of action
        """
        return self.dynamics.B.shape[1]

    def simulate(self, rng_key, n=1, T=100, x0=None, return_all=False):
        """ Simulate n trials

        Args:
            rng_key (jax.random.PRNGKey): random number generator key
            n (int): number of trials
            T (int): number of time steps
            x0 (jnp.array): initial state
            return_all (bool): return estimates, controls and observations as well

        Returns:
            jnp.array (T, n, d)
        """
        L = self.actor.L(T=T)
        K = self.actor.K(T=T)

        def simulate_trial(rng_key, T=100, x0=None, xhat0=None):
            """ Simulate a single trial

            Args:
                rng_key (jax.random.PRNGKey): random number generator key
                T (int): number of time steps
                x0 (jnp.array): initial state
                xhat0 (jnp.array): initial belief

            Returns:
                jnp.array, jnp.array, jnp.array, jnp.array: x (states), x_hat (estimates), y, u
            """

            x0 = jnp.zeros(self.xdim) if x0 is None else x0
            xhat0 = jnp.zeros(self.bdim) if xhat0 is None else xhat0

            # generate standard normal noise terms
            rng_key, subkey = random.split(rng_key)
            epsilon = random.normal(subkey, shape=(T, x0.shape[0]))
            rng_key, subkey = random.split(rng_key)
            eta = random.normal(subkey, shape=(T, x0.shape[0]))

            def loop(carry, t):
                x, x_hat = carry

                # compute control based on agent's current belief
                u = - L @ x_hat

                # apply dynamics
                x = self.dynamics.A @ x + self.dynamics.B @ u + self.dynamics.V @ epsilon[t]

                # generate observation
                y = self.dynamics.C @ x + self.dynamics.W @ eta[t]

                # update agent's belief
                x_pred = self.actor.A @ x_hat + self.actor.B @ u
                x_hat = x_pred + K @ (y - self.actor.C @ x_pred)

                return (x, x_hat), (x, x_hat, y, u)

            _, (x, x_hat, y, u) = scan(loop, (x0, xhat0), jnp.arange(1, T))

            return jnp.vstack([x0, x]), jnp.vstack([xhat0, x_hat]), \
                   jnp.vstack([self.dynamics.C @ x0 + self.dynamics.V @ eta[0]]), u

        # simulate n trials
        x, x_hat, y, u = vmap(lambda key: simulate_trial(key, T=T, x0=x0),
                              out_axes=1)(random.split(rng_key, num=n))

        if return_all:
            return x, x_hat, y, u
        else:
            return x

    def conditional_moments(self, x, mu0=None):
        """ Conditional distribution p(x | theta)

                Args:
                    self: LQG
                    x: time series of shape T (time steps), n (trials), d (dimensionality)

                Returns:
                    numpyro.distributions.MultivariateNormal
                """
        T, n, d = x.shape

        L = self.actor.L(T)

        K = self.actor.K(T)

        F = jnp.vstack(
            [jnp.hstack([self.dynamics.A,
                         -self.dynamics.B @ L]),
             jnp.hstack([K @ self.dynamics.C @ self.dynamics.A,
                         self.actor.A - self.actor.B @ L - K @ self.actor.C @ self.actor.A])])

        G = jnp.vstack([jnp.hstack([self.dynamics.V, jnp.zeros_like(self.dynamics.C.T)]),
                        jnp.hstack([K @ self.dynamics.C @ self.dynamics.V, K @ self.dynamics.W])])

        mu = jnp.zeros((n, self.dynamics.A.shape[0] + self.actor.A.shape[0])) if mu0 is None else mu0
        Sigma = G @ G.T

        def f(carry, xt):
            mu, Sigma = carry

            mu = mu @ F.T + (xt - mu[:, :d]) @ jnp.linalg.inv(Sigma[:d, :d]).T @ (F @ Sigma)[:, :d].T

            Sigma = F @ Sigma @ F.T + G @ G.T - (F @ Sigma)[:, :d] @ jnp.linalg.inv(Sigma[:d, :d]) @ (Sigma @ F.T)[:d,
                                                                                                     :]
            return (mu, Sigma), (mu, Sigma)

        _, (mu, Sigma) = scan(f, (mu, Sigma), x)
        return mu, Sigma

    def conditional_distribution(self, x):
        T, n, d = x.shape
        mu, Sigma = self.conditional_moments(x)
        return dist.MultivariateNormal(mu[:, :, :d].transpose((1, 0, 2)), Sigma[:, :d, :d])

    def log_likelihood(self, x):
        return self.conditional_distribution(x[:-1]).log_prob(x[1:].transpose((1, 0, 2)))


class LQG(System):
    def __init__(self, A, B, C, V, W, Q, R):
        dynamics = Dynamics(A, B, C, V, W)
        actor = Actor(A, B, C, V, W, Q, R)
        super().__init__(actor=actor, dynamics=dynamics)


if __name__ == '__main__':
    import matplotlib.pyplot as plt

    dt = 1. / 60.

    # parameters
    action_variability = 0.5
    sigma = 6.
    sigma_prop = 3.
    action_cost = 0.5

    A = jnp.eye(2)
    B = jnp.array([[0.], [dt]])
    V = jnp.diag(jnp.array([1., action_variability]))

    C = jnp.eye(2)
    W = jnp.diag(jnp.array([sigma, sigma_prop]))

    Q = jnp.array([[1., -1.],
                   [-1., 1]])

    R = jnp.eye(1) * action_cost

    lqg = System(actor=Actor(A, B, C, V, W, Q, R),
                 dynamics=Dynamics(A, B, C, V, W))

    x = lqg.simulate(random.PRNGKey(0), x0=jnp.zeros(2), n=10, T=1000)

    plt.plot(x[:, 0, 0])
    plt.plot(x[:, 0, 1])
    plt.show()
