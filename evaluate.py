
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from classifier import classify_ticket


BASE_DIR = Path(__file__).resolve().parent


async def main():
    load_dotenv()

    # Load our evaluation dataset.
    tickets = json.loads(
        (BASE_DIR / "samples/sample_tickets.json").read_text()
    )

    # Load reference labels.
    labels = json.loads(
        (BASE_DIR / "samples/reference_labels.json").read_text()
    )

    total = len(tickets)
    correct_category = 0
    correct_priority = 0
    errors = 0

    async with AsyncOpenAI(
        max_retries=0,
        timeout=20.0
    ) as client:

        for ticket in tickets:
            ticket_id = ticket["id"]
            expected = labels[ticket_id]

            try:
                result = await classify_ticket(client, ticket)

            except Exception as exc:
                errors += 1
                print(
                    f"{ticket_id}: FAILED "
                    f"({type(exc).__name__})"
                )
                continue

            category_match = (
                result.category == expected["category"]
            )

            priority_match = (
                result.priority == expected["priority"]
            )

            correct_category += int(category_match)
            correct_priority += int(priority_match)

            print(f"\nTicket: {ticket_id}")
            print(
                f"  Category: {result.category} "
                f"({'PASS' if category_match else 'MISMATCH'})"
            )
            print(
                f"  Priority: {result.priority} "
                f"({'PASS' if priority_match else 'MISMATCH'})"
            )
            print(f"  Summary: {result.summary}")

    print("\n========== EVALUATION RESULTS ==========")
    print(f"Tickets evaluated: {total}")
    print(f"Classification errors: {errors}")

    print(
        f"Category accuracy: "
        f"{correct_category}/{total} "
        f"({correct_category / total:.0%})"
    )

    print(
        f"Priority accuracy: "
        f"{correct_priority}/{total} "
        f"({correct_priority / total:.0%})"
    )


if __name__ == "__main__":
    asyncio.run(main())
