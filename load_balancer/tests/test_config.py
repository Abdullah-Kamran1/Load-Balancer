"""Task 5 (Phase 2): environment-driven configuration and strategy wiring."""

import pytest

from load_balancer.algorithms.round_robin import RoundRobinStrategy
from load_balancer.config import load_config


class TestBackendListParsing:
    def test_parses_id_host_port_list(self):
        cfg = load_config(
            {
                "LB_BACKENDS": "backend-1:127.0.0.1:8001, backend-2:127.0.0.1:8002",
            }
        )
        assert [(b.backend_id, b.host, b.port) for b in cfg.backends] == [
            ("backend-1", "127.0.0.1", 8001),
            ("backend-2", "127.0.0.1", 8002),
        ]

    def test_defaults_when_unset(self):
        cfg = load_config({})
        assert cfg.listen_host == "127.0.0.1"
        assert cfg.listen_port == 8080
        assert len(cfg.backends) >= 1  # a sensible default single backend

    def test_malformed_entry_raises(self):
        with pytest.raises(ValueError):
            load_config({"LB_BACKENDS": "backend-1:127.0.0.1:notaport"})


class TestStrategyWiring:
    def test_round_robin_selected_by_name(self):
        cfg = load_config({"LB_STRATEGY": "round_robin"})
        assert isinstance(cfg.strategy, RoundRobinStrategy)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError):
            load_config({"LB_STRATEGY": "magic_algorithm"})
