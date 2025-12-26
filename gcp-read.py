#!/usr/bin/env python3

# pylint: disable=invalid-name

""" Wiz : Resource Count : GCP """


import argparse
import concurrent.futures
import csv
import inspect
import os
import signal
import sys
import threading
import time
import warnings
import logging

# Suppress harmless httplib2 timeout warnings at multiple levels
warnings.filterwarnings('ignore', message='.*httplib2.*timeout.*')
logging.captureWarnings(True)
logging.getLogger('googleapiclient.http').setLevel(logging.ERROR)

# Suppress the specific httplib2 warning
os.environ['PYTHONWARNINGS'] = 'ignore::UserWarning'

# As a single script download, we do not publish a requirements.txt. Autodocument.

try:
    import googleapiclient.discovery
    import google.auth
except ImportError:
    print("\nERROR: Missing required GCP SDK packages. Run the following command to install/upgrade:\n")
    print("pip3 install --upgrade google-api-python-client")
    sys.exit(1)


version='2.8.3'


####
# Command Line Arguments
####


# For large-scale deployments (thousands of projects), increase max_workers significantly
# These are I/O-bound API calls, so high parallelism is safe and recommended
DEFAULT_MAX_WORKERS = 100

parser = argparse.ArgumentParser(description = 'Count GCP Resources')
parser.add_argument(
    '--all',
    action = 'store_true',
    dest = 'all',
    help = 'Count resources in all GCP Projects (default: disabled)',
    default = False
)
parser.add_argument(
    '--id',
    dest = 'id',
    help = 'Count resources in the specified GCP Project',
    default = None
)
parser.add_argument(
    '--projects',
    action = 'store_true',
    dest = 'input_projects',
    help = 'Count resources in the list of GCP projects (one ID per line) in a file named projects.txt (default: disabled)',
    default = False
)
parser.add_argument(
    '--exclude',
    action = 'store_true',
    dest = 'input_excluded_folders',
    help = 'Exclude folders in the list of GCP Folders (one ID per line) in a file named excluded-folders.txt (default: disabled)',
    default = False
)
parser.add_argument(
    '--data',
    action = 'store_true',
    dest = 'data_mode',
    help = 'Count Wiz Cloud Data Security (Buckets, Databases, etc) resources (default: disabled)',
    default = False
)
parser.add_argument(
    '--images',
    action = 'store_true',
    dest = 'images_mode',
    help = 'Count Wiz Cloud Registry Container Images (default: disabled)',
    default = False
)
parser.add_argument(
    '--max-image-tags',
    action = 'store',
    dest = 'max_image_tags',
    help = 'Number of image tags to count per registry image (default: 5, range 1 to 1000)',
    type = int,
    default = 5
)
parser.add_argument(
    '--max-workers',
    dest = 'max_workers',
    help = f'Maximum parallel processing requests (default: {DEFAULT_MAX_WORKERS}, range 1 to 1000)',
    type = int,
    default = DEFAULT_MAX_WORKERS
)
parser.add_argument(
    '--debug',
    action = 'store_true',
    dest = 'debug_mode',
    help = 'Disable parallel processing and exit upon first error (default: disabled)',
    default = False
)
parser.add_argument(
    '--verbose',
    action = 'store_true',
    dest = 'verbose_mode',
    help = 'Output verbose debugging information (default: disabled)',
    default = False
)
args = parser.parse_args()

if args.max_image_tags < 1 or args.max_image_tags > 1000:
    print(f"ERROR: --max-image-tags {args.max_image_tags} out of range: [1 .. 1000]")
    sys.exit(1)
if args.max_workers < 1 or args.max_workers > 1000:
    print(f"ERROR: --max-workers {args.max_workers} out of range: [1 .. 1000]")
    sys.exit(1)

####
# Configuration and Globals
####


excluded_folders_file = 'excluded-folders.txt'
input_file            = 'projects.txt'
output_file           = 'gcp-resources.csv'
output_file_log       = 'gcp-resources-log.csv'
error_log_file        = 'gcp-errors-log.txt'
padding = 6

# Map command-line arguments to counts to execute and display.
enabled = {
    'Virtual Machines':             True,
    'Container Hosts':              True,
    'Serverless Functions':         True,
    'Serverless Containers':        True,

    'Data Buckets':                 True,
    'PaaS Databases':               True,
    'Data Warehouses':              True,

    'Non-OS Disks':                 True,

    'Registry Container Images':    args.images_mode,

    'Kubernetes Sensors':           True,
    'Virtual Machine Sensors':      True,
    'Serverless Container Sensors': True,
}

totals = {
    'Virtual Machines':             0,
    'Container Hosts':              0,
    'Serverless Functions':         0,
    'Serverless Containers':        0,

    'Data Buckets':                 0,
    'PaaS Databases':               0,
    'Data Warehouses':              0,

    'Non-OS Disks':                 0,
    'Registry Container Images':    0,

    'Kubernetes Sensors':           0,
    'Virtual Machine Sensors':      0,
    'Serverless Container Sensors': 0,
}

totals_log = []
errors_log = []

# Thread locks for safe parallel project processing
totals_lock = threading.Lock()
totals_log_lock = threading.Lock()
errors_log_lock = threading.Lock()

try:
    google_auth_credential, _ = google.auth.default()
except Exception:  # pylint: disable=broad-exception-caught
    google_auth_credential = None

google_api_config = {
    'credentials': google_auth_credential,
    'num_retries': 3,
    'static_discovery': True
}


####
# Common Library Code
####


def signal_handler(_signal_received, _frame):
    """ Control-C """
    print("\nExiting")
    sys.exit(0)


def progress_print(resource_count, resource_type, project='', region='', details=''):
    """ Resource output """
    rc = str(resource_count).rjust(padding)
    # Split and join to remove multiple spaces when variables are empty.
    print(' '.join(f"- {rc} {resource_type} in {project} {region} {details}".split()))
    with totals_log_lock:
        totals_log.append([resource_type, resource_count, project, region])


def verbose_print(details):
    """ Verbose output """
    if args.verbose_mode:
        print(f"\nDEBUG: {details}")


def error_print(details, project = ''):
    """ Error output """
    project  = f"Project: {project} " if project else ""
    try:
        function = f"{inspect.stack()[1].function}()"
    except Exception:  # pylint: disable=broad-exception-caught
        function = ''
    try:
        details = str(details).replace("\n", " ").replace("\r", " ")
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    print(f"\nERROR: {project}{function} {details}\n")
    with errors_log_lock:
        errors_log.append(f"ERROR: {project}{function} {details}")


####
# Customized Library Code
####


def tag_in_tags(tag_key, tag_value, tags):
    """ Check for tag key and value """
    if not tags:
        return False
    return tags.get(tag_key) == tag_value


def label_in_labels(label, labels):
    """ Check for label in list """
    if not labels:
        return False
    return label in labels


def get_excluded_folders_from_file():
    """ Get the list of Excluded GCP Folders """
    excluded_folders = []
    if os.path.isfile(excluded_folders_file):
        with open(excluded_folders_file, encoding='utf-8') as f:
            excluded_folders = f.read().splitlines()
    else:
        error_print(excluded_folders_file + " does not exist.")
        error_print(f"Create a file named {excluded_folders_file} and add each GCP Folder ID to exclude, one per line.")
        error_print("Exiting...")
        sys.exit()
    excluded_folders.sort()
    verbose_print(f"excluded_folders: {excluded_folders}")
    return excluded_folders


def get_gcp_enabled_services(project_id):
    """ Get the list of enabled services for the specified Project """
    gcp_enabled_services = []
    try:
        client = googleapiclient.discovery.build('serviceusage', 'v1', **google_api_config)
        request = client.services().list(parent='projects/' + project_id, filter='state:ENABLED')
        while request is not None:
            response = request.execute()
            if 'services' in response:
                for item in response['services']:
                    gcp_enabled_services.append(item['config']['name'])
            if 'nextPageToken' in response:
                request = client.services().list_next(previous_request=request, previous_response=response)
            else:
                request = None
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
    client.close()
    gcp_enabled_services.sort()
    verbose_print(f"gcp_enabled_services: {gcp_enabled_services}")
    return gcp_enabled_services


def get_gcp_regions(project_id):
    """ Get GCP Regions for the specified Project """
    gcp_regions = []
    try:
        client = googleapiclient.discovery.build('compute', 'v1', **google_api_config)
        request = client.regions().list(project=project_id)
        while request is not None:
            response = request.execute()
            if 'items' in response:
                for region in response['items']:
                    gcp_regions.append(region['name'])
            if 'nextPageToken' in response:
                request = client.regions().list_next(previous_request=request, previous_response=response)
            else:
                request = None
        client.close()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
    #if not gcp_regions:
    #    error_print(f"No enabled regions found for Project {project_id}")
    gcp_regions.sort()
    verbose_print(f"gcp_regions: {gcp_regions}")
    return gcp_regions


# Subscriptions (aka GCP Projects)


def get_gcp_projects(excluded_folders):
    """ Get Active GCP Projects (ID, NAME) """
    gcp_projects = []
    try:
        client = googleapiclient.discovery.build('cloudresourcemanager', 'v1', **google_api_config)
        request = client.projects().list()
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex)
        error_print("Error getting GCP Projects.")
        return gcp_projects
    while request is not None:
        response = request.execute()
        if 'projects' in response:
            for project in response['projects']:
                if project['lifecycleState'] != 'ACTIVE':
                    verbose_print(f"- Skipping Inactive Project {project['projectId']}")
                    continue
                if 'parent' in project:
                    parent_folder = project['parent']['id']
                    if parent_folder in excluded_folders:
                        verbose_print(f"- Skipping Project {project['projectId']} in Excluded Folder {parent_folder}")
                        continue
                gcp_projects.append([project['projectId'], project.get('name', 'UNNAMED')])
        if 'nextPageToken' in response:
            request = client.projects().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()
    gcp_projects = sorted(gcp_projects, key=lambda p: p[0])
    verbose_print(f"gcp_projects: {gcp_projects}")
    return gcp_projects


# Subscriptions (aka GCP Projects) from local projects.txt file


def get_gcp_projects_from_file():
    """ Get the list of GCP Projects (ID) from a file named projects.txt """
    projects_ids = []
    gcp_projects = []
    if os.path.isfile(input_file):
        with open(input_file, encoding='utf-8') as f:
            for line in f:
                if len(line.strip()) > 0:
                    projects_ids.append(line.strip())
    else:
        error_print(input_file + " does not exist.")
        error_print(f"Create a file named {input_file} and add each GCP Project ID to scan, one per line.")
        error_print("Exiting...")
        sys.exit()

    # get project names
    for project_id in projects_ids:
        try:
            client = googleapiclient.discovery.build('cloudresourcemanager', 'v1', **google_api_config)
            request = client.projects().get(projectId=project_id)
            response = request.execute()
            gcp_projects.append([project_id, response.get('name', 'UNNAMED')])
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, project_id)
        client.close()
    gcp_projects = sorted(gcp_projects, key=lambda p: p[0])
    verbose_print(f"gcp_projects: {gcp_projects}")
    return gcp_projects


# Virtual Machines: Compute Instances and Container Hosts: GKE

# pylint: disable=too-many-locals, too-many-nested-blocks, too-many-statements
def get_gce_instances_and_gke_instances(project_id, project_name):
    """ Get GCP Compute and GKE Kubernetes Instances for the specified Project """
    instances_count = 0
    gke_instances_count = 0
    non_os_disks_count = 0
    linux_instances_count = 0
    try:
        client = googleapiclient.discovery.build('compute', 'v1', **google_api_config)
        request = client.instances().aggregatedList(project=project_id, maxResults=500)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'items' in response:
            for item in response['items']:
                # Loop over all GCP Zones in the response.
                zone_details = response['items'][item]
                if 'instances' in zone_details:
                    for instance in zone_details['instances']:
                        verbose_print(f"virtual_machine: {instance}")
                        if tag_in_tags('Vendor', 'Databricks', instance.get('tags', {})):
                            verbose_print(f"Skipping Databricks virtual_machine by tag: {instance['tags']}")
                            continue
                        if label_in_labels('databricks', instance.get('labels', [])):
                            verbose_print(f"Skipping Databricks virtual_machine by labels: {instance['labels']}")
                            continue
                        instances_count += 1
                        is_compute_instance = True
                        if 'labels' in instance:
                            for label in instance['labels']:
                                if label == 'goog-gke-node':
                                    gke_instances_count += 1
                                    is_compute_instance = False
                                    break
                        # Linux Sensor and Non-OS Disks are not applicable to GKE Instances.
                        if is_compute_instance and 'disks' in instance:
                            for disk in instance['disks']:
                                verbose_print(f"disk: {disk}")
                                if disk['boot']:
                                    disk_image_details = get_disk_image_details(client, project_id, disk)
                                    if 'description' not in disk_image_details:
                                        disk_image_details['description'] = 'UNKNOWN'
                                    if 'family' not in disk_image_details:
                                        disk_image_details['family'] = 'UNKNOWN'
                                    if 'win' not in disk_image_details['description'].lower() and 'win' not in disk_image_details['family'].lower():
                                        linux_instances_count += 1
                                else:
                                    non_os_disks_count += 1

        if 'nextPageToken' in response:
            request = client.instances().aggregatedList_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()

    if instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=instances_count, resource_type='Virtual Machines [Compute]', project=project_name, details=f"with {non_os_disks_count} Non-OS Disks")
        with totals_lock:
            totals['Virtual Machines'] += instances_count
            totals['Non-OS Disks'] += non_os_disks_count
            totals['Virtual Machine Sensors'] += linux_instances_count

    if gke_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=gke_instances_count, resource_type='Container Hosts [GKE]', project=project_name)
        with totals_lock:
            totals['Container Hosts'] += gke_instances_count
            totals['Kubernetes Sensors'] += gke_instances_count


def get_disk_image_details(client, project_id, disk):
    """ Get Compute Disk Image Details """
    image_detail = {}
    disk_zone = disk['source'].split('/')[-3]
    disk_name = disk['source'].split('/')[-1]
    try:
        disk_detail = client.disks().get(project=project_id, zone=disk_zone, disk=disk_name).execute()
        verbose_print(f"disk detail: {disk_detail}")
        image_name = disk_detail['sourceImage'].split('/')[-1]
        image_project = disk_detail['sourceImage'].split('/')[-4]
        image_detail = client.images().get(project=image_project, image=image_name).execute()
        verbose_print(f"disk image: {image_detail}")
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
    return image_detail


# Serverless Functions: Cloud Functions


def get_gcp_cloud_functions(project_id, project_name):
    """ Get GCP Cloud Functions for the specified Project """
    serverless_functions_count = 0
    try:
        client = googleapiclient.discovery.build('cloudfunctions', 'v2', **google_api_config)
        request = client.projects().locations().functions().list(parent='projects/' + project_id + '/locations/-')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'functions' in response:
            serverless_functions_count += len(response['functions'])
        if 'nextPageToken' in response:
            request = client.projects().locations().functions().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()

    if serverless_functions_count > 0 or args.verbose_mode:
        progress_print(resource_count=serverless_functions_count, resource_type='Serverless Functions [Cloud Functions]', project=project_name)
        with totals_lock:
            totals['Serverless Functions'] += serverless_functions_count


# Serverless Containers: Cloud Run Revisions


def get_gcp_cloudrun_revisions(project_id, project_name):
    """ Get GCP Cloud Run Revisions for the specified Project """
    serverless_containers_count = 0
    try:
        client = googleapiclient.discovery.build('run', 'v1', **google_api_config)
        request = client.namespaces().revisions().list(parent='namespaces/' + project_id, labelSelector='serving.knative.dev/revisionStatus=active')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'items' in response:
            for item in response['items']:
                for container in item['status']['conditions']:
                    if container['type'] == 'ContainerHealthy' and container['status'] == 'True':
                        serverless_containers_count += 1
        if 'nextPageToken' in response:
            request = client.namespaces().revisions().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()

    if serverless_containers_count > 0 or args.verbose_mode:
        progress_print(resource_count=serverless_containers_count, resource_type='Serverless Containers [Cloud Run Revisions]', project=project_name)
        with totals_lock:
            totals['Serverless Containers'] += serverless_containers_count


# Serverless Containers: GKE Autopilot

def get_gcp_gke_clusters(project_id, project_name):
    """ Get GCP Clusters for the specified Project """
    gke_nodes_count = 0
    gke_containers_count = 0
    try:
        client = googleapiclient.discovery.build('container', 'v1', **google_api_config)
        request = client.projects().zones().clusters().list(projectId=project_id, zone='-')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'clusters' in response:
            for cluster in response['clusters']:
                verbose_print(f"gke_cluster: {cluster}")
                if 'autopilot' in cluster and 'enabled' in cluster['autopilot']:
                    if cluster['autopilot']['enabled'] is True:
                        node_pools = cluster.get('nodePools', [])
                        for node_pool in node_pools:
                            node_count    = node_pool.get('currentNodeCount', node_pool.get('initialNodeCount', 0))
                            pods_per_node = node_pool.get('config', {}).get('maxPodsPerNode', 0)
                            gke_nodes_count      += node_count
                            gke_containers_count += node_count * pods_per_node
        if 'nextPageToken' in response:
            request = client.projects().zones().clusters().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()

    if gke_nodes_count > 0 or args.verbose_mode:
        progress_print(resource_count=gke_nodes_count, resource_type='Kubernetes Sensors [GKE Autopilot]', project=project_name)
        with totals_lock:
            totals['Kubernetes Sensors'] += gke_nodes_count

    if gke_containers_count > 0 or args.verbose_mode:
        progress_print(resource_count=gke_containers_count, resource_type='Serverless Containers [GKE Autopilot]', project=project_name)
        with totals_lock:
            totals['Serverless Containers'] += gke_containers_count


# Registry Container Images: GAR
# https://docs.wiz.io/wiz-docs/docs/supported-cloud-services
# Limits: 1000 Container Images per Container Registry


def get_gcp_gcr_images(project_id, project_name, region):
    """ Get GAR Container Images for the specified Project and Region """
    repositories = []
    container_registry_images = 0
    try:
        client = googleapiclient.discovery.build('artifactregistry', 'v1', **google_api_config)
        request = client.projects().locations().repositories().list(parent='projects/' + project_id + '/locations/' + region)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'repositories' in response:
            for item in response['repositories']:
                verbose_print(f"repository: {item}")
                if item['format'] == 'DOCKER':
                    repository = item['name'].split('/')[-1]
                    repositories.append(repository)
        if 'nextPageToken' in response:
            request = client.projects().locations().repositories().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    for repository in repositories:
        container_registry_images_in_repository = 0
        try:
            request = client.projects().locations().repositories().dockerImages().list(parent='projects/' + project_id + '/locations/' + region + '/repositories/' + repository)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            error_print(ex, project_id)
            continue
        while request is not None:
            response = request.execute()
            if 'dockerImages' in response:
                for image in response['dockerImages']:
                    verbose_print(f"image: {image}")
                    if 'tags' in image:
                        container_registry_images_in_repository += min(args.max_image_tags, len(image['tags']))
                    else:
                        container_registry_images_in_repository += 1
            if 'nextPageToken' in response:
                try:
                    request = client.projects().locations().repositories().dockerImages().list_next(previous_request=request, previous_response=response)
                except Exception as ex:  # pylint: disable=broad-exception-caught
                    error_print(ex, project_id)
                    continue
            else:
                request = None
        container_registry_images_in_repository = min(container_registry_images_in_repository, 10000)
        container_registry_images += container_registry_images_in_repository

    client.close()

    if container_registry_images > 0 or args.verbose_mode:
        progress_print(resource_count=container_registry_images, resource_type='Registry Container Images [GAR]', project=project_name, region=region)
        with totals_lock:
            totals['Registry Container Images'] += container_registry_images


# Data Buckets: Buckets
# https://docs.wiz.io/wiz-docs/docs/supported-cloud-services
# Limits: 10000 Storage Buckets per GCP Project


def get_gcp_buckets(project_id, project_name):
    """ Get GCP Buckets for the specified Project """
    buckets_count = 0
    try:
        client = googleapiclient.discovery.build('storage', 'v1', **google_api_config)
        request = client.buckets().list(project=project_id)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'items' in response:
            buckets_count += len(response['items'])
        if 'nextPageToken' in response:
            request = client.buckets().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()
    buckets_count = min(buckets_count, 10000)

    if buckets_count> 0 or args.verbose_mode:
        progress_print(resource_count=buckets_count, resource_type='Data Buckets', project=project_name)
        with totals_lock:
            totals['Data Buckets'] += buckets_count


# Data: PaaS Databases: Cloud SQL


def get_gcp_cloudsql_instances(project_id, project_name):
    """ Get GCP Cloud SQL Instances for the specified Project"""
    database_instances_count = 0
    try:
        client = googleapiclient.discovery.build('sqladmin', 'v1', **google_api_config)
        request = client.instances().list(project=project_id)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'items' in response:
            database_instances_count += len(response['items'])
        if 'nextPageToken' in response:
            request = client.instances().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()

    if database_instances_count > 0 or args.verbose_mode:
        progress_print(resource_count=database_instances_count, resource_type='PaaS Databases [Cloud SQL]', project=project_name)
        with totals_lock:
            totals['PaaS Databases'] += database_instances_count


# Data: PaaS Databases: Spanner


def get_gcp_spanner_instances(project_id, project_name):
    """ Get GCP Spanner Instances for the specified Project"""
    instances_databases_count = 0
    try:
        client = googleapiclient.discovery.build('spanner', 'v1', **google_api_config)
        request = client.projects().instances().list(parent=f'projects/{project_id}')
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'instances' in response:
            for instance in response['instances']:
                instances_databases_count += get_gcp_spanner_databases(client, instance['name'])
        if 'nextPageToken' in response:
            request = client.projects().instances().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()

    if instances_databases_count > 0 or args.verbose_mode:
        progress_print(resource_count=instances_databases_count, resource_type='PaaS Databases [Spanner]', project=project_name)
        with totals_lock:
            totals['PaaS Databases'] += instances_databases_count


##


def get_gcp_spanner_databases(client, instance_id):
    """ Get GCP Spanner Databases for the specified Instance"""
    database_count = 0
    try:
        request = client.projects().instances().databases().list(parent=instance_id)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, instance_id)
        return database_count
    while request is not None:
        response = request.execute()
        if 'databases' in response:
            database_count += len(response['databases'])
        if 'nextPageToken' in response:
            request = client.projects().instances().databases().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    return database_count


# Data: Data Warehouses: BigQuery


def get_gcp_bigquery_datasets(project_id, project_name):
    """ Get GCP BigQuery Tables for the specified Project"""
    data_warehouses_count = 0
    try:
        client = googleapiclient.discovery.build('bigquery', 'v2', **google_api_config)
        request = client.datasets().list(projectId=project_id)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        error_print(ex, project_id)
        return
    while request is not None:
        response = request.execute()
        if 'datasets' in response:
            data_warehouses_count += len(response['datasets'])
        if 'nextPageToken' in response:
            request = client.datasets().list_next(previous_request=request, previous_response=response)
        else:
            request = None
    client.close()

    if data_warehouses_count > 0 or args.verbose_mode:
        progress_print(resource_count=data_warehouses_count, resource_type='Data Warehouses [BigQuery]', project=project_name)
        with totals_lock:
            totals['Data Warehouses'] += data_warehouses_count


####
# Main
####

# pylint: disable=too-many-branches
def get_gcp_resources(project_id, project_name):
    """ Get billable resources for the specified Project """
    print(f"🔍 Scanning project: {project_id} ({project_name})...")
    exceptions = 0
    regions_list = []
    service_list = get_gcp_enabled_services(project_id)
    if 'compute.googleapis.com' in service_list:
        regions_list = get_gcp_regions(project_id=project_id)
    if not service_list:
        print(f"⏭️  Skipping: {project_id} (no services enabled)")
        return
    # If debug mode is disabled (default), run all functions concurrently with multithreading.
    # If debug mode is enabled, run all functions sequentially without multithreading.
    if args.debug_mode:
        if enabled['Virtual Machines'] or enabled['Container Hosts']:
            if 'compute.googleapis.com' in service_list:
                get_gce_instances_and_gke_instances(project_id=project_id, project_name=project_name)
        if enabled['Container Hosts'] or enabled['Serverless Containers']:
            if 'container.googleapis.com' in service_list:
                get_gcp_gke_clusters(project_id=project_id, project_name=project_name)
        if enabled['Serverless Functions']:
            if 'cloudfunctions.googleapis.com' in service_list:
                get_gcp_cloud_functions(project_id=project_id, project_name=project_name)
        if enabled['Serverless Containers']:
            if 'run.googleapis.com' in service_list:
                get_gcp_cloudrun_revisions(project_id=project_id, project_name=project_name)
        if enabled['Data Buckets']:
            if 'storage.googleapis.com' in service_list:
                get_gcp_buckets(project_id=project_id, project_name=project_name)
        if enabled['PaaS Databases']:
            if 'sqladmin.googleapis.com' in service_list:
                get_gcp_cloudsql_instances(project_id=project_id, project_name=project_name)
            if 'spanner.googleapis.com' in service_list:
                get_gcp_spanner_instances(project_id=project_id, project_name=project_name)
        if enabled['Data Warehouses']:
            if 'bigquery.googleapis.com' in service_list:
                get_gcp_bigquery_datasets(project_id=project_id, project_name=project_name)
        if enabled['Registry Container Images']:
            if 'artifactregistry.googleapis.com' in service_list:
                for region in regions_list:
                    get_gcp_gcr_images(project_id=project_id, project_name=project_name, region=region)
    else:
        futures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            if enabled['Virtual Machines'] or enabled['Container Hosts']:
                if 'compute.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gce_instances_and_gke_instances, project_id=project_id, project_name=project_name))
            if enabled['Container Hosts'] or enabled['Serverless Containers']:
                if 'container.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gcp_gke_clusters, project_id=project_id, project_name=project_name))
            if enabled['Serverless Functions']:
                if 'cloudfunctions.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gcp_cloud_functions, project_id=project_id, project_name=project_name))
            if enabled['Serverless Containers']:
                if 'run.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gcp_cloudrun_revisions, project_id=project_id, project_name=project_name))
            if enabled['Data Buckets']:
                if 'storage-api.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gcp_buckets, project_id=project_id, project_name=project_name))
            if enabled['PaaS Databases']:
                if 'sqladmin.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gcp_cloudsql_instances, project_id=project_id, project_name=project_name))
                if 'spanner.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gcp_spanner_instances, project_id=project_id, project_name=project_name))
            if enabled['Data Warehouses']:
                if 'bigquery.googleapis.com' in service_list:
                    futures.append(executor.submit(get_gcp_bigquery_datasets, project_id=project_id, project_name=project_name))
            if enabled['Registry Container Images']:
                if 'artifactregistry.googleapis.com' in service_list:
                    for region in regions_list:
                        futures.append(executor.submit(get_gcp_gcr_images, project_id=project_id, project_name=project_name, region=region))
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                exceptions += 1
    
    print(f"✅ Completed: {project_id}")


def output_results(projects):
    """ Output results """
    # Summary File
    with open(output_file, 'w', encoding='utf-8', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count'])
        for resource_type, resource_count in totals.items():
            csv_writer.writerow([resource_type, resource_count])
    # Log File
    with open(output_file_log, 'w', encoding='utf-8') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(['Resource Type', 'Resource Count', 'Project', 'Region'])
        for item in totals_log:
            csv_writer.writerow(item)

    # Error File
    if errors_log:
        with open(error_log_file, 'w', encoding='utf-8') as err_file:
            for error in errors_log:
                err_file.write(error + "\n")

    # Summary
    print(f"\nResults across {len(projects)} GCP Projects (script version: {version})\n")

    if enabled['Virtual Machines']:
        print(f"{str(totals['Virtual Machines']).rjust(padding)} Virtual Machines [Compute Instances]")
    if enabled['Container Hosts']:
        print(f"{str(totals['Container Hosts']).rjust(padding)} Container Hosts [GKE]")
    if enabled['Serverless Functions']:
        print(f"{str(totals['Serverless Functions']).rjust(padding)} Serverless Functions [Cloud Functions]")
    if enabled['Serverless Containers']:
        print(f"{str(totals['Serverless Containers']).rjust(padding)} Serverless Containers [Cloud Run Revisions, GKE Autopilot]")

    if enabled['Data Buckets']:
        print()
        print(f"{str(totals['Data Buckets']).rjust(padding)} Data Buckets (Public and Private) [Buckets]")
    if enabled['PaaS Databases']:
        print(f"{str(totals['PaaS Databases']).rjust(padding)} PaaS Databases [Cloud SQL, Spanner]")
    if enabled['Data Warehouses']:
        print(f"{str(totals['Data Warehouses']).rjust(padding)} Data Warehouses [BigQuery]")

    if enabled['Non-OS Disks']:
        print()
        print(f"{str(totals['Non-OS Disks']).rjust(padding)} Non-OS Disks [Compute Instances]")
    if enabled['Registry Container Images']:
        print()
        print(f"{str(totals['Registry Container Images']).rjust(padding)} Registry Container Images [GAR]")

    if enabled['Kubernetes Sensors']:
        print()
        print(f"{str(totals['Kubernetes Sensors']).rjust(padding)} Kubernetes Sensors")
    if enabled['Virtual Machine Sensors']:
        print(f"{str(totals['Virtual Machine Sensors']).rjust(padding)} Virtual Machine Sensors [Estimated from Virtual Machine Disk Image *]")

    if enabled['Virtual Machine Sensors']:
        print()
        print("* Linux Sensor counts may be lower, depending upon kernel and operating system versions")

    if not args.data_mode:
        print()
        print("To count Data Security (Buckets, Databases, etc) resources, rerun with '--data'")
    if not args.images_mode:
        print()
        print("To count Registry Container Images, rerun with '--images'")

    print(f"\nDetails written to {output_file} and {output_file_log}")

    if errors_log:
        print("\nExceptions occurred.")
        print(f"Review {error_log_file} or rerun with '--debug' to disable parallel processing and exit upon first error.")
    
    # Performance tip for large deployments
    if len(projects) > 100 and args.max_workers < 200:
        print("\n" + "="*80)
        print("PERFORMANCE TIP: For large deployments, increase parallelism for faster scans:")
        print(f"  Current: --max-workers {args.max_workers}")
        print(f"  Recommended for {len(projects)} projects: --max-workers 200-500")
        print("  Example: python3 resource-count-gcp-v2.py --all --max-workers 300")
        print("="*80)


def main():
    """ Calculon Compute! """
    projects = []

    excluded_folders = []
    if args.input_excluded_folders:
        print(f"Getting GCP Excluded Folders from {excluded_folders_file}\n")
        excluded_folders = get_excluded_folders_from_file()

    if args.all:
        print("Getting GCP Projects")
        projects = get_gcp_projects(excluded_folders)
        print(f"\n- Found {len(projects)} GCP Projects")
        for project in projects:
            print(f"-- {project[1]}")
        print('')
    elif args.input_projects:
        print(f"📂 Loading GCP Projects from file: {input_file}")
        projects = get_gcp_projects_from_file()
        print(f"✅ Loaded {len(projects)} projects from {input_file}")
    else:
        if args.id:
            print(f"Getting GCP Project {args.id}")
            projects = [[args.id, args.id]]
        else:
            project_id = input("Enter the GCP Project ID to scan: ")
            print('')
            projects = [[project_id, project_id]]

    print(f"\n{'='*80}")
    print(f"🚀 STARTING RESOURCE SCAN")
    print(f"{'='*80}")
    print(f"Total Projects: {len(projects)}")
    print(f"Parallel Workers: {args.max_workers}")
    print(f"Data Mode: {'Enabled' if args.data_mode else 'Disabled'}")
    print(f"Images Mode: {'Enabled' if args.images_mode else 'Disabled'}")
    print(f"{'='*80}\n")
    
    start_time = time.time()
    completed_count = 0
    progress_lock = threading.Lock()
    
    # Process projects in parallel unless debug mode is enabled
    if args.debug_mode:
        # Sequential processing for debugging
        for idx, (project_id, project_name) in enumerate(projects, 1):
            print(f"\n[{idx}/{len(projects)}] Scanning {project_id} ...")
            get_gcp_resources(project_id, project_name)
    else:
        # Parallel processing for better performance
        project_futures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            for project_id, project_name in projects:
                future = executor.submit(get_gcp_resources, project_id, project_name)
                project_futures[future] = (project_id, project_name)
            
            # Wait for all projects to complete with progress tracking
            for future in concurrent.futures.as_completed(project_futures):
                project_id, project_name = project_futures[future]
                try:
                    future.result()
                    with progress_lock:
                        completed_count += 1
                        elapsed = time.time() - start_time
                        rate = completed_count / elapsed if elapsed > 0 else 0
                        remaining = (len(projects) - completed_count) / rate if rate > 0 else 0
                        percentage = (completed_count / len(projects)) * 100
                        print(f"\n{'='*80}")
                        print(f"Progress: [{completed_count}/{len(projects)}] {percentage:.1f}% | "
                              f"Rate: {rate:.1f} proj/sec | "
                              f"ETA: {remaining/60:.1f} min")
                        print(f"{'='*80}\n")
                except Exception as ex:  # pylint: disable=broad-exception-caught
                    with progress_lock:
                        completed_count += 1
                    error_print(f"Exception scanning {project_id}: {ex}")
    
    total_time = time.time() - start_time
    print(f"\n{'='*80}")
    print(f"Completed scanning {len(projects)} projects in {total_time/60:.1f} minutes")
    print(f"Average rate: {len(projects)/total_time:.1f} projects/second")
    print(f"{'='*80}\n")

    output_results(projects)


if __name__ == '__main__':
    signal.signal(signal.SIGINT,signal_handler)
    main()
