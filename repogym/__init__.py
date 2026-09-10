"""RepoGym: turn everyday coding-agent sessions into a private RL gym.

Capture (fast, inside editor hooks)  ->  Build (async, verifies tests)  ->  Train (gym env).

Public entry points:
    repogym.env.RepoGymEnv      gym-style environment over a built task
    repogym.store.Store         on-disk store of episodes and tasks
    repogym.builder.build_episode
"""

__version__ = "0.1.0"

from .env import RepoGymEnv  # noqa: E402,F401
from .store import Store  # noqa: E402,F401
