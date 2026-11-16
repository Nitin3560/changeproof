import httpx
import psycopg

from changeproof.scenarios.generator import generate
from examples.payment_service.fake_gateway.app import create_app as gateway_app
from examples.payment_service.service import create_app
from examples.payment_service.storage import Store

from .runner import run_case
from .scheduler import Scheduler


async def run_benchmark(config, variant, *, dsn=None, timeout=15):
    """Exercise the instrumented sample with ASGI requests and real PostgreSQL state."""
    if variant not in {"vulnerable", "corrected"}:
        raise ValueError("Unknown service variant")
    if config.service.endpoint != "/payments":
        raise ValueError("The sample benchmark only supports /payments")
    store = Store(dsn)
    await store.initialize()
    gateway = httpx.ASGITransport(app=gateway_app(store))
    results = []
    for scenario in generate(config):
        scheduler = Scheduler(scenario.request_count if scenario.concurrent else 1)
        app = create_app(variant, store, gateway, hook=scheduler)

        async def read_effects(key):
            return await store.charges(variant, key)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://benchmark") as client:
            result = await run_case(client, scenario, read_effects, scheduler,
                                    endpoint=config.service.endpoint, timeout=timeout)
        results.append(result)
        if result.status == "error":
            break
    return results


def main(argv=None):
    import argparse
    import asyncio
    from collections import Counter

    from changeproof.invariants.loader import ConfigurationError, load_config

    parser = argparse.ArgumentParser(description="Run the instrumented PostgreSQL sample campaign")
    parser.add_argument("--variant", choices=["vulnerable", "corrected"], required=True)
    parser.add_argument("--config", default="configs/payment-idempotency.yaml")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        results = asyncio.run(run_benchmark(config, args.variant))
    except (ConfigurationError, ValueError, OSError, KeyError, psycopg.Error) as exc:
        print(f"Execution error: {exc}")
        return 2
    counts = Counter(result.status for result in results)
    print(f"{args.variant}: {len(results)} cases, {counts['violation']} violations, "
          f"{counts['error']} errors")
    for result in results:
        if result.error:
            print(result.error)
    print("Passing a bounded campaign does not prove correctness for all executions.")
    return 2 if counts["error"] else 1 if counts["violation"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
