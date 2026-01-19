import boto3
import uuid
from datetime import datetime
from urllib.parse import unquote_plus

# ================= AWS CLIENTS =================
s3 = boto3.client("s3")
rekognition = boto3.client("rekognition")
dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")

# ================= CONFIG =================
UPLOAD_BUCKET = "image-firewall-upload"
ALLOWED_BUCKET = "image-firewall-allowed"
QUARANTINE_BUCKET = "image-firewall-quarantine"

DYNAMO_TABLE = "image-firewall-audit"
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:070060394109:image-quarantine-alert"

table = dynamodb.Table(DYNAMO_TABLE)

# ================= AUDIT LOG =================
def log_audit(file_name, source_bucket, decision, reason, confidence):
    table.put_item(
        Item={
            "imageId": str(uuid.uuid4()),
            "fileName": file_name,
            "bucket": source_bucket,
            "decision": decision,
            "reason": reason,
            "confidence": str(confidence),
            "timestamp": datetime.utcnow().isoformat()
        }
    )

# ================= EMAIL ALERT =================
def send_email_alert(file_name, reason, confidence):
    message = f"""
🚨 IMAGE QUARANTINED 🚨

File Name : {file_name}
Reason    : {reason}
Confidence: {confidence}

Action: Review the image in quarantine bucket.
"""
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject="🚨 Image Firewall Alert",
        Message=message
    )

# ================= MOVE IMAGE =================
def move_image(src_bucket, dst_bucket, key):
    s3.copy_object(
        Bucket=dst_bucket,
        CopySource={"Bucket": src_bucket, "Key": key},
        Key=key
    )
    s3.delete_object(Bucket=src_bucket, Key=key)

# ================= LAMBDA HANDLER =================
def lambda_handler(event, context):
    try:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        print(f"Image received: {key}")

        if bucket != UPLOAD_BUCKET:
            return {"statusCode": 200}

        head = s3.head_object(Bucket=bucket, Key=key)
        size = head["ContentLength"]

        if size > 5 * 1024 * 1024:
            move_image(bucket, QUARANTINE_BUCKET, key)
            log_audit(key, bucket, "QUARANTINED", "SIZE_LIMIT_EXCEEDED", "N/A")
            send_email_alert(key, "SIZE_LIMIT_EXCEEDED", "N/A")
            return {"statusCode": 200}

        image_bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

        response = rekognition.detect_moderation_labels(
            Image={"Bytes": image_bytes},
            MinConfidence=50
        )

        labels = response.get("ModerationLabels", [])

        if labels:
            move_image(bucket, QUARANTINE_BUCKET, key)
            log_audit(key, bucket, "QUARANTINED", "NSFW_DETECTED", labels[0]["Confidence"])
            send_email_alert(key, "NSFW_DETECTED", labels[0]["Confidence"])
        else:
            move_image(bucket, ALLOWED_BUCKET, key)
            log_audit(key, bucket, "ALLOWED", "SAFE_CONTENT", 0)

        return {"statusCode": 200}

    except Exception as e:
        print("ERROR:", e)
        raise e
