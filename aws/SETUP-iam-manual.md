# AWS IAM: policy, user, and access key (console)

Use this with bucket **simon-kafka-csv-demo** and IAM user name **`svc-kafka-csv-s3-demo`**.

## 1. Create the policy

1. IAM → **Policies** → **Create policy** → **JSON** tab.
2. Paste the contents of `iam-policy-s3-simon-kafka-csv-demo.json` (in this folder).
3. **Next**: name the policy, for example: `kafka-csv-s3-demo-simon-bucket`
4. **Create policy**

## 2. Create the IAM user

1. IAM → **Users** → **Create user**
2. **User name:** `svc-kafka-csv-s3-demo` (or another name; keep it in notes for ops)
3. **Attach policies directly** → select `kafka-csv-s3-demo-simon-bucket` (the policy you created)
4. **Create user** (no console access needed for this machine user)

## 3. Create an access key

1. Open user **`svc-kafka-csv-s3-demo`**
2. **Security credentials** → **Create access key**
3. **Use case:** e.g. “Application running on AWS” or “Other” (per your org rules)
4. **Create** → **Download .csv** or copy **Access key ID** and **Secret access key** once

**Important:** The secret is shown only at creation. Store it in a password manager or your cluster secret store, not in Git.

## 4. OpenShift: put keys in a Secret (not in the ServiceAccount)

Static keys are stored in a **Kubernetes `Secret`**, and the `Deployment` references them. The `ServiceAccount` is only pod identity; it does not hold AWS static credentials.

```bash
oc -n kafka-demo create secret generic aws-s3-csv-creds \
  --from-literal=AWS_ACCESS_KEY_ID="AKIA..." \
  --from-literal=AWS_SECRET_ACCESS_KEY="...secret..."
```

Or apply `openshift/secret-aws-s3-csv.example.yaml` after replacing placeholders, then **delete the file with real values** from your disk if you used real strings.

**Never commit** real keys to the repository.

## 5. Bucket-side checks

- If the bucket uses **default SSE-S3** or you did not add a deny, this policy is usually enough.
- If the bucket enforces **SSE-KMS**, add KMS permissions in IAM (separate policy or statement) to match your CMK. This template does not include KMS.

## Note on `ListBucket`

The JSON policy only grants **object** read/write on `arn:aws:s3:::simon-kafka-csv-demo/*` (enough for `GetObject` / `HeadObject` / `PutObject` used by the demo containers). If you add tooling that calls `ListObjectsV2`, add a separate statement for `s3:ListBucket` on `arn:aws:s3:::simon-kafka-csv-demo` with a `s3:prefix` condition as needed.
