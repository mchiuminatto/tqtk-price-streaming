# Tick Price Emition

## Context

Generate synthetic tick price based on real data sample. The emission needs to follow a biased random walk.

The underlying process to generate the next period price is stochasticn and follows a normal distribution.

The variables to calculate the next value in the tick time series are the following

* Returns: Difference between current price and previous one.
* Volatility: Calculated from the return's standard deviation.
* Tick Frequency: How many ticks per time unit.
* Initial Price: Parameter
* Minimum Change Position: Position in the price value at which a unit change is incorporated. Inferred directly from the sample data: it is the position of the last (rightmost) decimal digit present in a sample price value. For example, for a value of 1.1405, the minimum change position is the 4th decimal place. For a value of 101.23, the minimum change position is the 2nd decimal place.

### How to calculate

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


| Parameter         | Description                                  | Source                        | Note |
| ----------------- | -------------------------------------------- | ----------------------------- | ---- |
| $\mu_I$           | Return's mean for instrument I               | Calculated from sample data   |      |
| $\sigma_I$        | Return's standard deviation for instrument I | Calculated from sample data   |      |
| $p_0$             | First price of the series for instrument I   | Obtained from sample data     |      |
| $unit{-}position$ | Position of the minimum changing value       | Obtained from the sample data |      |
| $\mu_{I_t}$       | Tick interval mean                           | Calculated from sample data   |      |
| $\sigma_{I_t}$    | Tick interval standard deviation             | Calculated from sample data   |      |
