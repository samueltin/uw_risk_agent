import asyncio
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8001/mcp") as client:

        # List all available tools
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        # Test flood zone — Bristol high-risk postcode
        result = await client.call_tool("get_flood_zone", {"postcode": "TW1 3NP"})
        print("\nFlood zone (TW1 3NP):", result)

        # Test low-risk postcode
        result = await client.call_tool("get_flood_zone", {"postcode": "TW3 3PB"})
        print("Flood zone (TW3 3PB):", result)

        # # Test high crime index
        # result = await client.call_tool("get_crime_index", {"postcode": "BS1 4DJ"})
        # print("\nCrime index:", result)


        # # Test low crime index
        # result = await client.call_tool("get_crime_index", {"postcode": "CW1 4TY"})
        # print("\nCrime index:", result)

        # Test claims history
        result = await client.call_tool("get_claims_history", {
            "applicant_name": "Jane Smith",
            "date_of_birth": "1978-06-15"
        })
        print("\nClaims history:", result)

        # Test validation — high risk submission
        import json
        submission = {
            "applicant_name": "Jane Smith",
            "date_of_birth": "1978-06-15",
            "occupation": "Teacher",
            "property_address": "12 Riverside Close, Bristol",
            "property_postcode": "BS1 4DJ",
            "property_type": "detached",
            "year_built": 1912,
            "construction": "timber",
            "num_storeys": 2,
            "product_type": "combined",
            "sum_insured": 425000.0,
            "policy_start_date": "2026-05-01",
            "claims_last_5_years": 2,
            "prior_claim_types": ["escape_of_water", "subsidence"],
            "outstanding_claims": False
        }
        result = await client.call_tool("validate_submission",
                                        {"submission_json": json.dumps(submission)})
        print("\nValidation:", result)

asyncio.run(main())