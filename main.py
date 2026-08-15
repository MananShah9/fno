import sys
import asyncio

async def run():
    if len(sys.argv) < 2:
        print("Usage: python main.py [worker|cli|trace|simulator]")
        print("No mode specified. Defaulting to starting the interactive CLI...")
        from cli import cli_main
        await cli_main()
        return

    mode = sys.argv[1].lower()
    if mode == "worker":
        from worker import worker_loop
        await worker_loop()
    elif mode == "cli":
        from cli import cli_main
        await cli_main()
    elif mode in ["trace", "diag", "diagnostics"]:
        import db
        db.init_db()
        import stage_tracker
        from cli import display_message_stage_timeline, view_recent_diagnostics_summary, view_stuck_messages, export_diagnostics_to_file

        args = sys.argv[2:]
        if not args or args[0] in ["--recent", "-r"]:
            view_recent_diagnostics_summary(interactive=False)
        elif args[0] in ["--stuck", "-s"]:
            view_stuck_messages(interactive=False)
        elif args[0] in ["--export", "-e"]:
            export_diagnostics_to_file(interactive=False)
        elif args[0].isdigit():
            display_message_stage_timeline(int(args[0]), is_tg_id=False, interactive=False)
        elif args[0].startswith("tg:") and args[0][3:].isdigit():
            display_message_stage_timeline(int(args[0][3:]), is_tg_id=True, interactive=False)
        else:
            print("Usage:")
            print("  python main.py trace <message_id>         # Inspect specific DB Message ID")
            print("  python main.py trace tg:<telegram_msg_id> # Inspect specific Telegram Message ID")
            print("  python main.py trace --recent             # View recent message health summary")
            print("  python main.py trace --stuck              # View stuck or errored messages")
            print("  python main.py trace --export             # Export diagnostics report to JSON")
    elif mode in ["sim", "simulator", "test"]:
        from test_simulator import run_simulation
        await run_simulation()
    else:
        print(f"Unknown mode: {mode}. Use 'worker', 'cli', 'trace', or 'simulator'.")

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting...")
