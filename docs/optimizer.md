# The optimizer

Thermaestro has two planners. **The rules** look at the hours around now: warm the rooms toward the top of their band while it is cheap, let them drift toward the bottom while it is expensive, charge the hot water in the cheapest window before it is needed. **The optimizer** plans the next two days at once, from what Thermaestro has learned of the house, the tank and the pump: how warm the rooms will be hour by hour, when the tank is charged, what the pump will use, and what that costs.

Both plan every 15 minutes. One drives, the one chosen under **Setup → Control**: the rules, until the household switches. The other's decisions are kept beside, and the **Plan** page shows the two side by side. Thermaestro never switches planners by itself.

## What it needs

The optimizer plans a goal only once what it rests on has been learned and has proved itself (see **Setup → Learned**):

- **a climate system:** how its rooms follow the heating and the weather, how its water follows the offset, and what its radiators or floor give and store; a room with a fresh temperature; the compressor's COP (learned, or as documented) and what it draws;
- **a hot-water tank:** its standing loss and its week of hot-water use, and its volume;
- **a pool:** how fast it warms and cools.

Whatever it can't plan yet stays with the rules, and so does everything when a plan fails or takes over 30 seconds. **Setup → Control** lists what it covers and why the rest is left out. A climate system without a room sensor stays with the rules too: nothing measures how its rooms answer.

## How it plans

The horizon is 48 hours in 15-minute slots. Where tomorrow's prices aren't published yet, the price is estimated from the same quarter of the week over the last four weeks, and the plan firms up when they come.

- **The house** follows its learned model hour by hour: the rooms pulled toward the heating water and the outdoor temperature, the building's mass behind them. The forecast's learned bias is taken off the outdoor temperature.
- **The water** follows the heating offset, keeping a share of where it was an hour before: a slab in the floor takes hours. The pump gives the heat the water hands the house and what warms the radiators or floor themselves; a slab cooling heats the rooms with heat stored earlier.
- **The compressor** serves one thing at a time: the heating, the tank or the pool share each slot. Its heat is its draw times its COP at that slot's brine and water temperatures. The addition gives heat at a COP of 1, as the household's policy for it allows. Where neither of its settings is in Thermaestro's hands, the pump runs it on its own, and the plan counts it as the pump does: it gives what the compressor can't (the pump's degree minutes then run deep and start it), at most half its power, since the pump steps it up only as they deepen, and nothing while the pump's mean outdoor temperature is at its stop or above.
- **The tank** loses heat as learned and is drawn from as its week expects; **a pool** warms and cools as learned.

The plan stays where its models know the house: the offset no further than a step past what it was learned on, raised at most a step an hour, and never far ahead of where the water can follow (beyond that, the pump's degree minutes run deep and its addition starts; the plan counts that heat at a COP of 1). Each room is planned inside its band by a margin of twice the house model's own error six hours ahead, from 0.25 to 0.75 °C.

Each plan starts from the rooms' mean over the last hour, as the house model was learned, not from the air as it swings with each run of the compressor. And it carries a drift: a share of how far the rooms came out from what the plan an hour before expected for them, so a model a little off is corrected by what it sees. The brine and the household's own load (the house's power less the pump's) are the last hour's means too, and the tank's temperatures as they stood a minute ago: everything measured is read only up to the last minute surely written, so a reading that came in a moment before the round or a moment after doesn't change the plan, and shadow plans as control would.

## What gives way

Every goal the household sets is soft, so a plan always exists and says what falls short. The plan is solved in stages, each keeping what the stages before it reached:

1. the hot water's floor, and what the pump physically can give;
2. the household's ranking, in its order: by default rooms not below their band, hot water that must be ready, hot water that should be ready, rooms not above their band, the power limit;
3. the pools' bands;
4. last, cost against closeness to the middle of each band, by the savings slider. Without a cost stance or prices, nothing moves by price: the rooms are kept near the middle of their bands.

What each stage leaves short is shown on the Plan page, in its own unit: degree-hours below a band, degrees short at a deadline, kWh over a limit.

## How each intent stands

After each slot's plan, every intent it plans is judged against it, over the next day (further ahead, the forecast and the prices are guesses):

- **at risk** where the plan expects a miss: a room planned more than 0.1 °C below its band for an hour or more, not counting the hours its heat needs to show (from when the band last rose, or from now), or the hot water planned more than 0.3 °C short at a deadline;
- **giving way** where that miss is a higher goal's doing: the plan is solved once more with the short goal's stage ranked first, and if it is met then, the intent was giving way, to the stages above it that came out worse. A goal already ranked first can't give way: it is at risk;
- otherwise **active**, as planned met.

An intent is changed, and told as an event, only when its state or what it gives way to changes: the plan's figures move every slot. The house's own measurements give what is missed: a room below its band for over half an hour once its heat could show, the hot water below its floor, a bath not met by its latest. See [the Events page](setup.md#what-happened-the-events-page).

## From the plan to the settings

The heating offset takes whole steps, and every change is a write. Over the next four hours, the optimizer picks the steps that come closest to the plan, half a step either side counting as on it, each change costing a step held wrong for an hour and a half, and no more changes than what is left of half the day's write budget, after the changes since midnight, spread over the hours left. With a room below its band now, the offset isn't lowered, and with one above it, it isn't raised, whatever the plan expects. A tank is charged with one extra charge where the plan charges it now for a hot-water deadline. For price, its own charges are held off while the plan charges it no sooner than an hour on, and let go once the plan charges it within the next slot (only with a cost stance and prices; otherwise the pump's own thermostat stands): a charge isn't started for price alone, so the rooms keep the compressor. Near its floor, every hold is let go, and below it a charge is started, whatever the plan. A pool's start temperature is set just above where it is when the plan heats it now. The addition is kept off where the plan doesn't use it, unless the household leaves it to the pump.

**Compressor starts** stay at the pump's own pace: a charge that would start a standing compressor waits for it to run while the day's starts are ahead of the pace the pump kept on its own (learned from the last week), unless the hot water's floor, a room below its band or hot water that must be ready needs it. A compressor that stopped within twenty minutes isn't started for price.

## Checking it

The **Plan** page shows the plan ahead as a chart (the price, the rooms, the tank, the pump's kW), what it leaves short, both planners' last decisions side by side with the one driving marked, how many changes each asked for, and how the optimizer's predictions of the rooms came out one, three and six hours ahead. [Replaying](replay.md) a recorded period runs the learners over it.
