#!/usr/bin/env python3
"""One-time backfill of TXT owner records for existing asg-dns-handler slots.

Seeds the companion TXT owner record the Lambda now writes on UPSERT, so the
explicit ownership path is active on every current slot without waiting for a
rotation. Safe to re-run; dry-run unless --apply is passed.

For each ASG tagged `asg:hostname_pattern`, finds the live (pending/running)
instance holding each hostname's A record and UPSERTs TXT = that instance-id.
"""

import argparse
import boto3

HOSTNAME_TAG_NAME = "asg:hostname_pattern"


def instance_ip(instance, use_public):
    return instance.get('PublicIpAddress') if use_public else instance.get('PrivateIpAddress')


def fetch_record(route53, zone_id, hostname, record_type):
    sets = route53.list_resource_record_sets(
        HostedZoneId=zone_id, StartRecordName=hostname,
        StartRecordType=record_type, MaxItems='1'
    )['ResourceRecordSets']
    if not sets:
        return None
    record = sets[0]
    if record['Name'].rstrip('.').lower() != hostname.rstrip('.').lower() or record['Type'] != record_type:
        return None
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--asg', help='Only process ASGs whose name contains this substring')
    parser.add_argument('--public-ip', action='store_true',
                        help='Match on public IPs (mirror the Lambda USE_PUBLIC_IP=true)')
    parser.add_argument('--apply', action='store_true', help='Write TXT records (default: dry-run)')
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    autoscaling, ec2, route53 = session.client('autoscaling'), session.client('ec2'), session.client('route53')

    paginator = autoscaling.get_paginator('describe_auto_scaling_groups')
    for page in paginator.paginate():
        for asg in page['AutoScalingGroups']:
            name = asg['AutoScalingGroupName']
            if args.asg and args.asg not in name:
                continue
            pattern = next((t['Value'] for t in asg['Tags'] if t['Key'] == HOSTNAME_TAG_NAME), None)
            if not pattern or '@' not in pattern:
                continue
            hostname_pattern, zone_id = pattern.split('@')

            instance_ids = [i['InstanceId'] for i in asg['Instances']]
            if not instance_ids:
                continue
            reservations = ec2.describe_instances(
                InstanceIds=instance_ids,
                Filters=[{'Name': 'instance-state-name', 'Values': ['pending', 'running']}]
            )['Reservations']

            for reservation in reservations:
                for instance in reservation['Instances']:
                    instance_id = instance['InstanceId']
                    ip = instance_ip(instance, args.public_ip)
                    hostname = hostname_pattern.replace('#instanceid', instance_id)
                    a_record = fetch_record(route53, zone_id, hostname, 'A')
                    if a_record is None or not ip:
                        continue
                    if a_record['ResourceRecords'][0]['Value'] != ip:
                        continue  # live instance isn't the current A holder; leave it alone

                    existing = fetch_record(route53, zone_id, hostname, 'TXT')
                    if existing and existing['ResourceRecords'][0]['Value'].strip('"') == instance_id:
                        continue  # already owned

                    print("%s %s -> owner %s" % ("WOULD SET" if not args.apply else "SETTING", hostname, instance_id))
                    if args.apply:
                        route53.change_resource_record_sets(
                            HostedZoneId=zone_id,
                            ChangeBatch={'Changes': [{
                                'Action': 'UPSERT',
                                'ResourceRecordSet': {
                                    'Name': hostname, 'Type': 'TXT', 'TTL': a_record['TTL'],
                                    'ResourceRecords': [{'Value': '"%s"' % instance_id}]
                                }
                            }]}
                        )


if __name__ == "__main__":
    main()
