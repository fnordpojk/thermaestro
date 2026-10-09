"""A scenario: one house, its pump and its weather, drawn from a seed.

The plant's parameters are drawn per scenario from plausible ranges, so the planner is
never tested against its own assumptions. A scenario is plain data: it is saved and read
as JSON, so a failing case can be kept as a file.
"""

import json
import random
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Emitter = Literal["radiators", "floor"]


@dataclass(frozen=True)
class House:
    """One climate system's zone: indoor air, the building's mass, and the emitter."""

    ua_w_k: float
    """Heat lost through the envelope per kelvin indoors over outdoors."""
    c_air_j_k: float
    """Air and furniture."""
    c_mass_j_k: float
    """Walls and floors."""
    ua_mass_w_k: float
    emitter: Emitter
    c_emitter_j_k: float
    """Radiators and their water, or a floor slab: a slab answers over a day or more."""
    ua_emitter_w_k: float
    solar_m2: float
    """The sun's effective aperture."""
    wind_w_k_per_m_s: float
    """Extra loss per m/s of wind."""
    internal_w: float
    """People and appliances."""
    start_c: float = 21.0


@dataclass(frozen=True)
class Tank:
    liters: float = 180.0
    top_share: float = 0.4
    """How much of the volume the top node (BT7) holds; the rest is BT6's."""
    loss_w_k: float = 1.5
    cold_c: float = 8.0
    room_c: float = 18.0
    """Where the tank stands."""
    start_c: float = 48.0


@dataclass(frozen=True)
class Draw:
    hour: float
    """Local hour of the day it starts."""
    liters: float
    minutes: float


@dataclass(frozen=True)
class Pool:
    m3: float = 30.0
    loss_w_k: float = 250.0
    start_c: float = 24.0


@dataclass(frozen=True)
class Weather:
    mean_c: float
    swing_c: float
    """Half the difference between a day's warmest and coldest."""
    drift_c: float
    """How far the daily mean wanders, at most."""
    clear: float
    """The share of clear days."""
    wind_m_s: float
    ground_c: float = 6.0


@dataclass(frozen=True)
class Load:
    base_kw: float
    """The household's electricity, apart from the pump."""
    cooking_kw: float


@dataclass(frozen=True)
class Scenario:
    seed: int
    start: str
    """ISO time, UTC, of the first moment."""
    houses: tuple[House, ...]
    weather: Weather
    tank: Tank = field(default_factory=Tank)
    draws: tuple[Draw, ...] = ()
    pool: Pool | None = None
    load: Load = field(default_factory=lambda: Load(0.35, 2.0))
    heat_kw: float = 6.0
    """The compressor's heat at 0 °C brine and 35 °C supply (an F1245-6)."""
    registers: dict[int, int] = field(default_factory=dict)
    """Settings that differ from the plant's defaults, as raw register words."""

    @property
    def start_time(self) -> datetime:
        return datetime.fromisoformat(self.start)

    def set(self, registers: dict[int, int]) -> "Scenario":
        """The same scenario with these settings."""
        return replace(self, registers={**self.registers, **registers})

    # --- as data ---------------------------------------------------------------------------

    def to_json(self) -> str:
        data = asdict(self)
        data["registers"] = {str(k): v for k, v in self.registers.items()}
        return json.dumps(data, indent=1, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Scenario":
        data = json.loads(text)
        return cls(
            seed=data["seed"],
            start=data["start"],
            houses=tuple(House(**h) for h in data["houses"]),
            weather=Weather(**data["weather"]),
            tank=Tank(**data["tank"]),
            draws=tuple(Draw(**d) for d in data["draws"]),
            pool=Pool(**data["pool"]) if data["pool"] else None,
            load=Load(**data["load"]),
            heat_kw=data["heat_kw"],
            registers={int(k): v for k, v in data["registers"].items()},
        )

    def save(self, path: Path) -> None:
        path.write_text(self.to_json() + "\n")

    @classmethod
    def read(cls, path: Path) -> "Scenario":
        return cls.from_json(path.read_text())


def draw(
    seed: int,
    *,
    start: datetime = datetime(2026, 1, 15, tzinfo=UTC),
    systems: int = 1,
    emitter: Emitter | None = None,
    pool: bool = False,
    mean_c: float | None = None,
) -> Scenario:
    """A scenario from plausible ranges. What is fixed is fixed; the rest is drawn."""
    rng = random.Random(seed)
    houses = []
    for _ in range(systems):
        choices: tuple[Emitter, ...] = ("radiators", "floor")
        kind = emitter if emitter is not None else rng.choice(choices)
        floor = kind == "floor"
        houses.append(
            House(
                ua_w_k=rng.uniform(120, 280),
                c_air_j_k=rng.uniform(1.5e6, 3.5e6),
                c_mass_j_k=rng.uniform(15e6, 50e6),
                ua_mass_w_k=rng.uniform(600, 2000),
                emitter=kind,
                c_emitter_j_k=rng.uniform(25e6, 60e6) if floor else rng.uniform(0.2e6, 0.6e6),
                ua_emitter_w_k=rng.uniform(500, 900) if floor else rng.uniform(250, 450),
                solar_m2=rng.uniform(2, 8),
                wind_w_k_per_m_s=rng.uniform(2, 8),
                internal_w=rng.uniform(200, 500),
                start_c=rng.uniform(20.5, 21.5),
            )
        )
    draws = []
    for hour, liters in ((6.5, 45.0), (7.2, 15.0), (12.0, 8.0), (18.5, 20.0), (21.0, 50.0)):
        if rng.random() < 0.85:
            draws.append(
                Draw(
                    hour=hour + rng.uniform(-0.5, 0.5),
                    liters=liters * rng.uniform(0.6, 1.4),
                    minutes=rng.uniform(3, 10),
                )
            )
    return Scenario(
        seed=seed,
        start=start.isoformat(),
        houses=tuple(houses),
        weather=Weather(
            mean_c=rng.uniform(-8, 6) if mean_c is None else mean_c,
            swing_c=rng.uniform(1, 5),
            drift_c=rng.uniform(1, 6),
            clear=rng.uniform(0.1, 0.6),
            wind_m_s=rng.uniform(1, 7),
            ground_c=rng.uniform(4, 8),
        ),
        tank=Tank(start_c=rng.uniform(45, 50)),
        draws=tuple(draws),
        pool=Pool(m3=rng.uniform(20, 50), start_c=rng.uniform(23, 26)) if pool else None,
        load=Load(base_kw=rng.uniform(0.2, 0.6), cooking_kw=rng.uniform(1.5, 3.0)),
        heat_kw=rng.uniform(5.0, 7.0),
    )
