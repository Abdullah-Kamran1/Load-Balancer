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

    def test_least_active_requests_selected_by_name(self):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        cfg = load_config({"LB_STRATEGY": "least_active_requests"})
        assert isinstance(cfg.strategy, LeastActiveRequestsStrategy)

    def test_ip_hash_selected_by_name(self):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        cfg = load_config({"LB_STRATEGY": "ip_hash"})
        assert isinstance(cfg.strategy, IpHashStrategy)

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValueError):
            load_config({"LB_STRATEGY": "magic_algorithm"})


class TestHealthConfigWiring:
    def test_health_defaults_match_monitor(self):
        cfg = load_config({})
        assert cfg.health.interval == 1.0
        assert cfg.health.timeout == 1.0
        assert cfg.health.failure_threshold == 3
        assert cfg.health.recovery_threshold == 2

    def test_health_env_vars_override_defaults(self):
        cfg = load_config(
            {
                "LB_HEALTH_INTERVAL": "0.5",
                "LB_HEALTH_TIMEOUT": "2",
                "LB_HEALTH_FAILURES": "5",
                "LB_HEALTH_RECOVERIES": "3",
            }
        )
        assert cfg.health.interval == 0.5
        assert cfg.health.timeout == 2.0
        assert cfg.health.failure_threshold == 5
        assert cfg.health.recovery_threshold == 3

    def test_health_non_numeric_raises(self):
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_INTERVAL": "fast"})

    def test_health_non_positive_raises(self):
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_TIMEOUT": "0"})
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_FAILURES": "0"})
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_RECOVERIES": "-1"})

    def test_health_non_finite_raises(self):
        # NaN passes the <= 0 check and would silently disable probing
        # (interval=nan -> sleep(nan) stalls the loop forever); inf is
        # equally meaningless. Both must fail fast at startup.
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_INTERVAL": "nan"})
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_INTERVAL": "inf"})
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_TIMEOUT": "nan"})
        with pytest.raises(ValueError):
            load_config({"LB_HEALTH_TIMEOUT": "-inf"})

    def test_duplicate_backend_ids_raise(self):
        # duplicate ids key the monitor's per-backend probe tasks; the
        # second start() would overwrite the first handle and leak an
        # unstoppable task. Ids must be unique.
        with pytest.raises(ValueError):
            load_config(
                {"LB_BACKENDS": "a:127.0.0.1:8001,a:127.0.0.1:8002"}
            )
