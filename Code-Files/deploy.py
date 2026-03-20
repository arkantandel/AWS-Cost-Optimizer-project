import boto3, json, zipfile, io, time, argparse, sys

CONFIG = {
    "region": "us-east-1",
    "phone_number": "+91XXXXXXXXXX",
    "project_name": "cost-optimizer",
    "scan_schedule": "rate(6 hours)",
    "cpu_threshold": 5,
    "idle_days": 7
}

# ================= SCANNER CODE =================
SCANNER_CODE = '''
import boto3, os
from datetime import datetime, timedelta, timezone

DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
SNS_TOPIC_ARN  = os.environ["SNS_TOPIC_ARN"]
CPU_THRESHOLD  = float(os.environ.get("CPU_THRESHOLD", "5"))
IDLE_DAYS      = int(os.environ.get("IDLE_DAYS", "7"))

ec2 = boto3.client("ec2")
cw = boto3.client("cloudwatch")
sns = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)

def lambda_handler(event, context):
    findings = scan_ec2()

    for f in findings:
        table.put_item(Item=f)

    if findings:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=f"AWS ALERT: {len(findings)} idle resources"
        )

    return {"count": len(findings)}

def scan_ec2():
    results = []
    scan_date = datetime.now(timezone.utc).isoformat()

    resp = ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )

    for r in resp["Reservations"]:
        for i in r["Instances"]:
            iid = i["InstanceId"]
            cpu = get_avg(iid)

            if cpu < CPU_THRESHOLD:
                results.append({
                    "resourceId": iid,
                    "scanDate": scan_date,
                    "resourceType": "EC2",
                    "resourceName": iid,
                    "detail": f"CPU {cpu}",
                    "estimatedMonthlyCost": 10,
                    "status": "PENDING"
                })
    return results

def get_avg(instance_id):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=IDLE_DAYS)

    resp = cw.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Average"]
    )

    pts = resp["Datapoints"]
    return sum(d["Average"] for d in pts) / len(pts) if pts else 0
'''

# ================= HELPERS =================
def make_zip(code):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("lambda_function.py", code)
    return buf.getvalue()

def deploy():
    region = CONFIG["region"]
    p = CONFIG["project_name"]

    lam = boto3.client("lambda", region_name=region)
    dynamo = boto3.client("dynamodb", region_name=region)
    sns = boto3.client("sns", region_name=region)
    events = boto3.client("events", region_name=region)
    iam = boto3.client("iam")

    # 1. IAM Role
    role = iam.create_role(
        RoleName=f"{p}-role",
        AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow","Principal": {"Service": "lambda.amazonaws.com"},"Action": "sts:AssumeRole"}]
        })
    )
    role_arn = role["Role"]["Arn"]

    iam.put_role_policy(
        RoleName=f"{p}-role",
        PolicyName="policy",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow","Action": "*","Resource": "*"}]
        })
    )

    time.sleep(10)

    # 2. DynamoDB
    table_name = f"{p}-findings"
    dynamo.create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "resourceId", "AttributeType": "S"},
            {"AttributeName": "scanDate", "AttributeType": "S"}
        ],
        KeySchema=[
            {"AttributeName": "resourceId", "KeyType": "HASH"},
            {"AttributeName": "scanDate", "KeyType": "RANGE"}
        ]
    )

    time.sleep(5)

    # 3. SNS
    topic = sns.create_topic(Name=f"{p}-alerts")
    topic_arn = topic["TopicArn"]

    sns.subscribe(
        TopicArn=topic_arn,
        Protocol="sms",
        Endpoint=CONFIG["phone_number"]
    )

    # 4. Lambda
    lam.create_function(
        FunctionName=f"{p}-scanner",
        Runtime="python3.12",
        Role=role_arn,
        Handler="lambda_function.lambda_handler",
        Code={"ZipFile": make_zip(SCANNER_CODE)},
        Environment={"Variables": {
            "DYNAMODB_TABLE": table_name,
            "SNS_TOPIC_ARN": topic_arn,
            "CPU_THRESHOLD": str(CONFIG["cpu_threshold"]),
            "IDLE_DAYS": str(CONFIG["idle_days"])
        }}
    )

    # 5. EventBridge
    rule = events.put_rule(
        Name=f"{p}-rule",
        ScheduleExpression=CONFIG["scan_schedule"],
        State="ENABLED"
    )

    events.put_targets(
        Rule=f"{p}-rule",
        Targets=[{"Id": "1","Arn": lam.get_function(FunctionName=f"{p}-scanner")["Configuration"]["FunctionArn"]}]
    )

    print("✅ DEPLOYED SUCCESSFULLY")

if __name__ == "__main__":
    deploy()
