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
    print("🚀 Scanner started")

    findings = scan_ec2()

    for item in findings:
        table.put_item(Item=item)

    if findings:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=f"AWS ALERT 🚨 {len(findings)} idle resources found"
        )

    return {
        "statusCode": 200,
        "count": len(findings)
    }


def scan_ec2():
    results = []
    scan_date = datetime.now(timezone.utc).isoformat()

    response = ec2.describe_instances(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )

    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:

            instance_id = instance["InstanceId"]
            cpu = get_avg_cpu(instance_id)

            print(f"{instance_id} CPU: {cpu}")

            if cpu < CPU_THRESHOLD:
                results.append({
                    "resourceId": instance_id,
                    "scanDate": scan_date,
                    "resourceType": "EC2",
                    "resourceName": instance_id,
                    "detail": f"Low CPU: {round(cpu,2)}%",
                    "estimatedMonthlyCost": 10,
                    "status": "PENDING"
                })

    return results


def get_avg_cpu(instance_id):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=IDLE_DAYS)

    response = cw.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=86400,
        Statistics=["Average"]
    )

    datapoints = response["Datapoints"]

    if not datapoints:
        return 0

    return sum(d["Average"] for d in datapoints) / len(datapoints)
