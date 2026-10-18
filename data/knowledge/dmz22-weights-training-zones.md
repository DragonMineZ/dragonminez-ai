# DMZ v2.2 weights and weight training zones

Weights (Turtle Shell, Workout Weights, Weighted Cape) go in the Weights Curios slot. Ask Master Roshi, King Kai or Piccolo for their weight service and type the kg you want (max 100,000 kg, once per conversation). Admins: `/dmzweight turtle_shell|workout_weights|piccolo_cape <kg>`.

The game compares effective weight (worn weight x gravity load factor) with your ideal weight (grows with your level). Load ratio = effective / ideal:
- up to 0.75: Comfortable, x1.0 to x2.0 TP, no penalty.
- 0.75 to 1.25: Ideal, x2.0 TP, no penalty (recommended).
- 1.25 to 2.5: Pushing Hard, up to x2.5 TP but a rising stat penalty (up to -60%, also slower movement and attacks).
- 2.5+: Overloaded, x1.0 TP and -60% stats.
Heavier zones also raise attack stamina cost and form drain (x1.15 Comfortable, x1.3 Ideal, x1.5 Pushing Hard, x2.0 Overloaded). As you level up, ask for heavier weights. The Gravity row of the Stats menu (V) shows your weights, ideal weight, zone and multiplier.

Gravity amplifies weight, so weights plus a Gravity Device or the Time Chamber is the fastest training setup. Config: `gravity` section (`tpComfort*`, `tpIdeal*`, `tpOverload*`, `maxWeightPenalty`, `loadDrain*`, `maxWeightRequested`).

Keywords: weights, weight, turtle shell, workout weights, weighted cape, piccolo cape, weight training, ideal weight, overloaded, training zone, dmzweight
