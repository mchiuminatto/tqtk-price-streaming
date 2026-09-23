# Tick Price Emitter

## Context

Generate synthetic tick price based on real data sample. The emission needs to follow a biased random walk.

The underlying process to generate the next period price is stochastic and follows a distribution that is particular for each symbol.

The estimated values are:

- Bid price: Estimated based on the corresponding tick returns distribution for each instrument.
- Spread: Estimated based on the spread distribution for each instrument.
- Ask price: Calculated as Bid + Spread
- Time interval: Next tick timestamp calculated on the interval distribution corresponding for each instrument.

## How to calculate

As stated above, four calculations are necessary:

### t+1

$\Large t_{I_{t+1}} = D_{TI}(parameters_{TI})$

Where:

- $t_{I_{t+1}}$ Is the timestamp for the next tick for the instrument I.
- $D_{TI}(parameters_{TI})$ Is the time interval distribution, and its parameters, for the instrument I. Each instrument's family and parameters are defined in the calibration seed script, `deploy/calibration/calibration.redis` (its tick-interval section), and loaded into the calibration store the adapter reads.

### Bid.

$\Large bid_{I_{t+1}} = bid_{I_{t}} + D_{RI}(parameters_{RI})$

Where:

- $bid_{I_{t+1}}$: Bid price at timestamp t+1 for instrument I
- $bid_{I_{t}}$: Bid price at timestamp t for instrument I
- $D_{RI}(parameters_{RI})$: Tick return's distribution, and its parameters for instrument I. Each instrument's family and parameters are defined in the calibration seed script, `deploy/calibration/calibration.redis` (its return section), and loaded into the calibration store the adapter reads.

Note:

The addition must be done at the minimum change decimal position. See: Minimum change position below.

### Spread.

$Spread_{I_{t+1}} = D_{SI}(parameters_{SI})$

Where:

- $Spread_{I_{t+1}}$: Spread at timestamp t+1 for instrument I.
- $D_{SI}(parameters_{SI})$: Spread distribution and its parameters for instrument I. Each instrument's family and parameters are defined in the calibration seed script, `deploy/calibration/calibration.redis` (its spread section), and loaded into the calibration store the adapter reads.

### Ask.

$Ask_{I_{t+1}} = Bid_{I{t+1}} + Spread_{I{t+1}}$

## Minimum change position.

Is the decimal position where a change in one unit (pip, contract) is reflected. Each instrument's value is its `pip_size`, defined in the calibration seed script, `deploy/calibration/calibration.redis`, with the method used to find it.

## Constraints

For precision purposes DO NOT use float as datatype for prices, instead work with decimal standard library.
