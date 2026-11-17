"""
Serverless AWS Cost Estimator with History and Budget Alerts
Services used: AWS Lambda, DynamoDB, SNS, CloudWatch, IAM

POST  -> calculate estimated monthly cost, save to DynamoDB, send SNS alert if over budget
GET   -> return last 10 saved estimates

Environment variables (set in Lambda > Configuration > Environment variables):
    TABLE_NAME    = name of the DynamoDB table (partition key: id, type String)
    TOPIC_ARN     = ARN of the SNS topic (email subscription)
    BUDGET_LIMIT  = monthly budget in USD, e.g. 50

NOTE: Prices are approximate (US East - N. Virginia, On-Demand).
Verify with https://calculator.aws
"""

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")

TABLE_NAME = os.environ.get("TABLE_NAME", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
BUDGET_LIMIT = float(os.environ.get("BUDGET_LIMIT", "50"))

# ---------------- Price list (approximate) ----------------
PRICES = {
    "ec2_per_hour": {"t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416},
    "s3_per_gb_month": 0.023,
    "lambda_per_million_requests": 0.20,
    "lambda_per_gb_second": 0.0000166667,
    "data_transfer_per_gb": 0.09,
}
FREE = {
    "ec2_hours": 750,
    "s3_gb": 5,
    "lambda_requests": 1_000_000,
    "lambda_gb_seconds": 400_000,
    "data_transfer_gb": 100,
}


# ---------------- Calculation functions ----------------
def calc_ec2(hours, instance_type, free):
    rate = PRICES["ec2_per_hour"].get(instance_type)
    if rate is None:
        raise ValueError(f"Unsupported instance type: {instance_type}")
    billable = max(hours - (FREE["ec2_hours"] if free else 0), 0)
    return round(billable * rate, 2)


def calc_s3(gb, free):
    billable = max(gb - (FREE["s3_gb"] if free else 0), 0)
    return round(billable * PRICES["s3_per_gb_month"], 2)


def calc_lambda(requests, memory_mb, duration_ms, free):
    gb_seconds = requests * (memory_mb / 1024) * (duration_ms / 1000)
    billable_requests = max(requests - (FREE["lambda_requests"] if free else 0), 0)
    billable_gbs = max(gb_seconds - (FREE["lambda_gb_seconds"] if free else 0), 0)
    request_cost = (billable_requests / 1_000_000) * PRICES["lambda_per_million_requests"]
    compute_cost = billable_gbs * PRICES["lambda_per_gb_second"]
    return round(request_cost + compute_cost, 2)


def calc_transfer(gb, free):
    billable = max(gb - (FREE["data_transfer_gb"] if free else 0), 0)
    return round(billable * PRICES["data_transfer_per_gb"], 2)


# ---------------- Helpers ----------------
def to_json(obj):
    """Convert DynamoDB Decimal values so json.dumps works."""
    return json.dumps(obj, default=lambda o: float(o) if isinstance(o, Decimal) else str(o))


def response(code, data):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": to_json(data),
    }


# ---------------- GET: history ----------------
def get_history():
    table = dynamodb.Table(TABLE_NAME)
    items = table.scan(Limit=50).get("Items", [])
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return response(200, {"count": len(items[:10]), "history": items[:10]})


# ---------------- POST: estimate ----------------
def estimate(body):
    try:
        free = bool(body.get("apply_free_tier", False))
        ec2 = calc_ec2(float(body.get("ec2_hours", 0)), body.get("ec2_type", "t3.micro"), free)
        s3 = calc_s3(float(body.get("s3_gb", 0)), free)
        lam = calc_lambda(float(body.get("lambda_requests", 0)),
                          float(body.get("lambda_memory_mb", 128)),
                          float(body.get("lambda_duration_ms", 100)), free)
        transfer = calc_transfer(float(body.get("data_transfer_gb", 0)), free)
    except (ValueError, TypeError) as e:
        return response(400, {"error": str(e)})

    total = round(ec2 + s3 + lam + transfer, 2)
    over_budget = total > BUDGET_LIMIT

    result = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "currency": "USD",
        "free_tier_applied": free,
        "breakdown": {"EC2": ec2, "S3": s3, "Lambda": lam, "DataTransfer": transfer},
        "total_monthly_cost": total,
        "budget_limit": BUDGET_LIMIT,
        "over_budget": over_budget,
    }

    # Save to DynamoDB (floats must be converted to Decimal)
    try:
        item = json.loads(json.dumps(result), parse_float=Decimal)
        dynamodb.Table(TABLE_NAME).put_item(Item=item)
    except Exception as e:
        print(f"ERROR saving to DynamoDB: {e}")

    # Send SNS email alert if over budget
    if over_budget and TOPIC_ARN:
        try:
            sns.publish(
                TopicArn=TOPIC_ARN,
                Subject="AWS Budget Alert",
                Message=f"Estimated cost ${total} is above your budget of ${BUDGET_LIMIT}.\n"
                        f"Breakdown: {result['breakdown']}",
            )
            print("Budget alert sent")
        except Exception as e:
            print(f"ERROR sending SNS alert: {e}")

    print(f"Estimate {result['id']} total=${total} over_budget={over_budget}")
    return response(200, result)


# ---------------- Entry point ----------------
def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    if method == "GET":
        return get_history()

    body = event.get("body", event)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return response(400, {"error": "Invalid JSON"})
    return estimate(body)
