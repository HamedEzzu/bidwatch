"""BidWatch runner: one cycle on demand, or on a schedule."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

import agent as bidwatch
from config import MAX_POSTINGS_PER_RUN, MODEL_ID, REGION, RUN_INTERVAL_MINUTES, SCORE_THRESHOLD, TAG
from tools.notify import telegram_configured

logger = logging.getLogger("bidwatch.scheduler")


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("strands").setLevel(logging.WARNING)


def run_cycle(mode: str, tag: str, dry_run: bool, interactive: bool = False) -> None:
    """Run exactly one BidWatch cycle and log what it did."""
    started = time.time()
    logger.info(
        "Cycle start — mode=%s tag=%s dry_run=%s threshold=%d max_postings=%d",
        mode, tag, dry_run, SCORE_THRESHOLD, MAX_POSTINGS_PER_RUN,
    )

    if mode == "agent" and not dry_run and not interactive:
        summary = bidwatch.run_agent_loop(tag=tag)
        logger.info("Agent summary: %s", summary)
    else:
        if interactive and mode == "agent":
            logger.info("--interactive drives the console bid flow, so this cycle runs in pipeline mode.")
        stats = bidwatch.run_pipeline(tag=tag, dry_run=dry_run, interactive=interactive)
        logger.info(
            "Cycle results — fetched=%d new=%d scored=%d notifications=%d",
            stats["fetched"], stats["new"], stats["scored"], stats["notified"],
        )

    usage = bidwatch.usage()
    if usage["calls"]:
        logger.info(
            "Approx. token usage this process — calls=%d input=%d output=%d",
            usage["calls"], usage["input_tokens"], usage["output_tokens"],
        )
    logger.info("Cycle end — %.1fs elapsed", time.time() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description="BidWatch — AI Remote job scout.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run a single cycle against bundled fixtures with zero model calls.",
    )
    parser.add_argument(
        "--mode",
        choices=("agent", "pipeline"),
        default="agent",
        help="'agent' lets the model drive the Strands tool loop; 'pipeline' runs the same steps deterministically in Python.",
    )
    parser.add_argument("--tag", default=TAG, help=f"Remote OK tag to monitor (default: {TAG}).")
    parser.add_argument(
        "--interval",
        type=int,
        default=RUN_INTERVAL_MINUTES,
        help=f"Minutes between scheduled cycles (default: {RUN_INTERVAL_MINUTES}).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Console bid flow: prompt [b]id/[s]kip/[o]pen per job (no Telegram needed).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args()

    load_dotenv()
    configure_logging(args.verbose)
    logger.info("BidWatch starting — model=%s region=%s", MODEL_ID, REGION)
    if not args.dry_run and telegram_configured():
        logger.info("Telegram is configured. Run 'python bot.py' alongside this to handle button taps.")

    if args.dry_run or args.once:
        run_cycle(mode=args.mode, tag=args.tag, dry_run=args.dry_run, interactive=args.interactive)
        return 0

    # Be polite to the source: never poll faster than every 15 minutes.
    interval = max(15, args.interval)
    if interval != args.interval:
        logger.warning("Interval raised to %d minutes to stay polite to the job source.", interval)
    logger.info("Scheduled mode — running every %d minutes. Ctrl-C to stop.", interval)
    try:
        while True:
            run_cycle(mode=args.mode, tag=args.tag, dry_run=False, interactive=args.interactive)
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
