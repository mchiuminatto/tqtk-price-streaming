# Tick Price Emition

## Context

Generate synthetic tick price based on real data sample. The emission needs to follow a biased random walk.

The underlying process to generate the next period price is stochastic and follows a distribution that is particular for each symbol.

The estimated values are:

Bid price: Estimated based on the corresponding tick returns distribution for each instrument.

Spread: Estimated based on the spread distribution for each instrument.

Ask price: Calculated as Bid + Spread

Time interval: Next tick timestamp calculated on the interval distribution corresponding for rach instrument.


## How to calculate

As stated above, four calculations are necessary.

Let be:

- $p_0$ the initial price of the series.
- $\mu_I$: Mean of returns for the instrument I
- $\sigma_I$ Standard deviation for returns for instrument I
- $p_t$ Price at period t
- $p_{t+1}: Pirce at petiod t+1

To claculate the next price

$\Large p_t+1 = p_t + r_{t+1}$

Where $\Large r_{t+1} \leftarrow N(\mu_I, \sigma_I)$

Is the return obtained from the normal distribution $N$ of mean $\mu_I$ and standard deviation $\sigma_I$

The trend can be controlled using the value of $\mu_I$ as follows:

$$
Trend_I =

\begin {cases}Long & \mu_I > 0 \\

Range & \mu_I = 0 \\

Short & \mu_I < 0

\end{cases}
$$

## Generation Frequency

Calculate the frequecny at which tick are generated as follows:

Let be:

- $\Large \mu_{I_t}$ the mean interval at which ticks are recorded.
- $\Large \sigma_{I_t}$ the standrad deviation of the intervals at which ticks are recorded.
- $t_i$, timestamp of the tick at time t

The timestamp for the next tick is claculated as follows:

$\Large t_{t+1} = \delta_{I_t} + t_i $

Where:

$\Large \delta_{I_t} <- N_I(\mu_{I_t}, \sigma_{I_t})$

Where $N_I$ is the normal distribution for tick intervals for instrument I, with mean $\mu_{I_t}$ and standard deviation $\sigma_{I_t}$

## Parameters

The following table describes the model parameters and how are obtained.


| Parameter         | Description                                  | Source                        | Data type | Note                                                           |
| ----------------- | -------------------------------------------- | ----------------------------- | --------- | -------------------------------------------------------------- |
| $\mu_I$           | Return's mean for instrument I               | Calculated from sample data   | float     | Feeds the normal-distribution sampler, which is float-only     |
| $\sigma_I$        | Return's standard deviation for instrument I | Calculated from sample data   | float     | Feeds the normal-distribution sampler, which is float-only     |
| $p_0$             | First price of the series for instrument I   | Obtained from sample data     | Decimal   | A price value - see Constraints                                |
| $unit{-}position$ | Position of the minimum changing value       | Obtained from the sample data | Decimal   | Stored as the increment value (e.g.`0.0001`), not the position |
| $\mu_{I_t}$       | Tick interval mean                           | Calculated from sample data   | float     | A time value, not a price                                      |
| $\sigma_{I_t}$    | Tick interval standard deviation             | Calculated from sample data   | float     | A time value, not a price                                      |

## Constraints

For precision purposes DO NOT use float as datatype for prices, instead work with decimal standard library.

This applies to $p_t$, $p_{t+1}$, $r_{t+1}$, $p_0$, and the minimum-change-position increment: every
price value and every value derived directly from a price (a return, the running walk state, the
rounded/published price) is a `Decimal`, never a `float`. $\mu_I$ and $\sigma_I$ are the exception:
they parameterize the normal distribution sampler, which is float-only, so the sampled return is
converted to `Decimal` immediately after being drawn, before it is added to the price.
