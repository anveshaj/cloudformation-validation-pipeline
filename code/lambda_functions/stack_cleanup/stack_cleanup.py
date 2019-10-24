import boto3
from datetime import datetime
from datetime import timedelta
import json
from random import randrange
from time import sleep


iam_client = boto3.client('iam')


def get_regions(region, service):
    if region == 'ALL':
        s = boto3.session.Session(region_name='us-east-1')
        return s.get_available_regions(service)
    else:
        return [region]


def get_all_pipelines(region):
    regions = get_regions(region, 'codepipeline')
    pipelines = {}
    for region in regions:
        cp_client = boto3.client('codepipeline', region_name=region)
        response = cp_client.list_pipelines()
        for pipeline in response['pipelines']:
            matched = False
            pl_detail = cp_client.get_pipeline(name=pipeline['name'])
            for stage in pl_detail['pipeline']['stages']:
                for action in stage['actions']:
                    if 'UserParameters' in action['configuration']:
                        try:
                            params = json.loads(action['configuration']['UserParameters']).keys()
                            if 'CleanupNonFailed' in params and 'StackCreationRoleArn' in params:
                                matched = True
                        except ValueError as e:
                            if e.args[0] != 'No JSON object could be decoded':
                                raise

            if matched:
                pipelines[pipeline['name']] = region
    return pipelines


def _describe_stacks(cfn_client, stackname, retries=10, backoff=1.2, delay=5, jitter=True):
    while retries > 0:
        retries -= 1
        try:
            return cfn_client.describe_stacks(StackName=stackname)
        except Exception as e:
            if "Rate exceeded" in e.response['Error']['Message']:
                if jitter:
                    delay = int(delay * backoff) + randrange(0, 10)
                else:
                    delay = int(delay * backoff)
                sleep(delay)
            else:
                raise


def get_all_stacks():
    regions = get_regions('ALL', 'cloudformation')
    stacks = {}
    for region in regions:
        stacks[region] = []
        cfn_client = boto3.client('cloudformation', region_name=region)
        response = cfn_client.list_stacks(StackStatusFilter=[
            'CREATE_FAILED', 'CREATE_COMPLETE', 'ROLLBACK_COMPLETE',
            'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE', 'DELETE_FAILED'])
        for stack in response['StackSummaries']:
            describe_response = _describe_stacks(cfn_client, stack['StackName'])
            for tag in describe_response['Stacks'][0]['Tags']:
                if tag['Key'] == 'cfn_cicd_pipeline':
                    stacks[region].append({
                        'name': stack['StackName'], 'pipeline': tag['Value'],
                        'status': stack['StackStatus'], 'created': stack['CreationTime'].replace(tzinfo=None),
                        'tags': describe_response['Stacks'][0]['Tags'], 'region': region})
    return stacks


def get_all_keypairs():
    regions = get_regions('ALL', 'ec2')
    key_pairs = {}
    for region in regions:
        key_pairs[region] = []
        ec2_client = boto3.client('ec2', region_name=region)
        response = ec2_client.describe_key_pairs()
        for kp in response['KeyPairs']:
            if kp['KeyName'].startswith('ci-'):
                key_pairs[region].append(kp['KeyName'])
    return key_pairs


def iter_stacks(stacks, filter_func, filter_val):
    filtered_stacks = {}
    for region in stacks.keys():
        filtered_stacks[region] = []
        for stack in stacks[region]:
            if filter_func(stack, filter_val):
                filtered_stacks[region].append(stack)
    return filtered_stacks


def filter_pipeline_name(stack, pipeline_name):
    for tag in stack['tags']:
        if tag['Key'] == 'cfn_cicd_pipeline' and tag['Value'] == pipeline_name:
            return True
    return False


def filter_failed(stack, failed):
    if stack['status'] not in ['CREATE_FAILED', 'ROLLBACK_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']:
            return True
    return False


def filter_age(stack, age):
    min_age = datetime.utcnow() - timedelta(days=age)
    if stack['created'] < min_age:
            return True
    return False


def filter_latest(stack, pipelines):
    pipeline_name = None
    execution_id = None
    for tag in stack['tags']:
        if tag['Key'] == 'cfn_cicd_pipeline':
            pipeline_name = tag['Value']
        elif tag['Key'] == 'cfn_cicd_executionid':
            execution_id = tag['Value']
    if pipeline_name not in pipelines.keys():
        return False
    cp_client = boto3.client('codepipeline', region_name=pipelines[pipeline_name])
    response = cp_client.get_pipeline_state(name=pipeline_name)
    if response['stageStates'][0]['latestExecution']['pipelineExecutionId'] == execution_id:
        return False
    return True


def delete_stacks(stacks):
    for region in stacks.keys():
        if len(stacks[region]) > 0:
            cfn_client = boto3.client('cloudformation', region_name=region)
            for stack in stacks[region]:
                print('deleting stack %s in %s from pipeline %s' % (stack['name'], region, stack['pipeline']))
                try:
                    cfn_client.delete_stack(StackName=stack['name'])
                except Exception as e:
                    if e.args[0].endswith('is invalid or cannot be assumed'):
                        try:
                            arn = get_role_arn()
                            cfn_client.delete_stack(StackName=stack['name'], RoleARN=arn)
                        except Exception as e:
                            print('Failed to delete stack %s' % (str(stack)))
                            print(str(e))
                    else:
                        print('Failed to delete stack %s' % (str(stack)))
                        print(str(e))


def get_role_arn():
    try:
        return iam_client.get_role(RoleName='TemplateCI-StackCleanUp')['Role']['Arn']
    except Exception:
        return "NoValidRoles"


def delete_keypairs(region, keypairs):
    ec2_client = boto3.client('ec2', region_name=region)
    for kp in keypairs:
        ec2_client.delete_key_pair(KeyName=kp)


def lambda_handler(event, context):
    print(event)
    pipeline = event['pipeline']
    region = event['region']
    age = int(event['age'])
    failed = event['failed']
    latest = event['latest']

    print('Getting stacks...')
    stacks = get_all_stacks()

    print("Cleanup orphaned stacks...")
    orphaned = {}
    pipelines = get_all_pipelines('ALL')
    for region in stacks.keys():
        for stack in stacks[region]:
            if stack['pipeline'] not in pipelines.keys():
                try:
                    orphaned[region].append(stack)
                except Exception:
                    orphaned[region] = [stack]
                print(stack['pipeline'], pipelines.keys())
                print("stack %s is orphaned" % stack['name'])
    delete_stacks(orphaned)

    print("Cleanup keypairs...")
    key_pairs = get_all_keypairs()
    for region in key_pairs.keys():
        kp_to_delete = []
        for kp in key_pairs[region]:
            stack_list = [s['name'] for s in stacks[region]]
            if kp not in stack_list:
                kp_to_delete.append(kp)
        if len(kp_to_delete) > 0:
            delete_keypairs(region, kp_to_delete)

    print('getting pipelines...')
    pipelines = get_all_pipelines(region)
    filtered_stacks = stacks
    if pipeline:
        print('Filtering results to specific pipeline')
        filtered_stacks = iter_stacks(filtered_stacks, filter_pipeline_name, pipeline)
    if not failed:
        print('Filtering results to exclude failed stacks')
        filtered_stacks = iter_stacks(filtered_stacks, filter_failed, failed)
    if age > 0:
        print('Filtering results to exclude stacks older than %s days' % str(age))
        filtered_stacks = iter_stacks(filtered_stacks, filter_age, age)
    if latest:
        print('Filtering results to exclude most recent pipeline execution')
        filtered_stacks = iter_stacks(filtered_stacks, filter_latest, pipelines)
    delete_stacks(filtered_stacks)
from awsclients import AwsClients
import boto3
from cfnpipeline import CFNPipeline
from logger import Logger
import os


message = """\
A pipeline action has failed:
Pipeline: %s
Region: %s
Stage: %s
Action: %s
Link: %s
Error: %s
"""
link_template = "https://%s.console.aws.amazon.com/codepipeline/home?region=%s#/view/%s"

table_name = os.environ['table_name']

loglevel = 'debug'
logger = Logger(loglevel=loglevel)
logger.info('New Lambda container initialised, logging configured.')
clients = AwsClients(logger)
pipeline_run = CFNPipeline(logger, clients)


def get_pipeline_failures(pipeline, region):
    cp_client = boto3.client('codepipeline', region_name=region)
    pl_state = cp_client.get_pipeline_state(name=pipeline)
    print(pl_state)
    issues = []
    for stage in pl_state['stageStates']:
        for action in stage['actionStates']:
            if 'latestExecution' in action.keys():
                if action['latestExecution']['status'] == 'Failed':
                    stage_name = stage['stageName']
                    action_name = action['actionName']
                    error = action['latestExecution']['errorDetails']['message']
                    lastchange = action['latestExecution']['lastStatusChange'].replace(
                        tzinfo=None
                    ).strftime('%s')
                    issues.append([stage_name, action_name, error, lastchange])
    if len(issues) > 0:
        return issues
    return None


def is_new_issue(error_id, lastchange):
    ddb_table = boto3.resource('dynamodb').Table(table_name)
    response = ddb_table.get_item(Key={"FailureId": error_id})
    if 'Item' in response.keys():
        if response['Item']['LastChange'] == lastchange:
            return False
    ddb_table.put_item(Item={'FailureId': error_id, 'LastChange': lastchange})
    return True


def lambda_handler(event, context):
    print(event)
    pipeline = event['pipeline']
    region = event['region']
    topic = event['topic']
    sns_region = topic.split(':')[3]

    issues = get_pipeline_failures(pipeline, region)
    if issues:
        for stage, action, error, lastchange in issues:
            error_id = "%s::%s::%s::%s" % (region, pipeline, stage, action)
            if is_new_issue(error_id, lastchange):
                sns_client = boto3.client('sns', region_name=sns_region)
                subject = "Pipeline Failure - %s - %s" % (pipeline, stage)
                link = link_template % (region, region, pipeline)
                body = message % (pipeline, region, stage, action, link, error)
                body += '\n\n'
                body += pipeline_run.build_execution_report(
                    pipeline_id=pipeline, region=region,
                    sns_topic=None, s3_bucket=None, s3_prefix='',
                    s3_region=None, execution_id=None
                )
                sns_client.publish(TopicArn=topic, Subject=subject[:100], Message=body)
