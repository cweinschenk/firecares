from firecares.celery import app
from django.db import connections
from django.db.utils import ConnectionDoesNotExist
from firecares.firestation.models import FireDepartment, create_quartile_views
from firecares.firestation.models import NFIRSStatistic as nfirs
from fire_risk.models import DIST, NotEnoughRecords
from fire_risk.models.DIST.providers.ahs import ahs_building_areas
from fire_risk.models.DIST.providers.iaff import response_time_distributions
from fire_risk.backends.queries import RESIDENTIAL_FIRES_BY_FDID_STATE
from fire_risk.utils import LogNormalDraw
from firecares.utils import dictfetchall


def update_scores():
    for fd in FireDepartment.objects.filter(archived=False):
        update_performance_score.delay(fd.id)


@app.task(queue='update')
def update_performance_score(id, dry_run=False):
    """
    Updates department performance scores.
    """

    try:
        cursor = connections['nfirs'].cursor()
        fd = FireDepartment.objects.get(id=id)
    except (ConnectionDoesNotExist, FireDepartment.DoesNotExist):
        return

    cursor.execute(RESIDENTIAL_FIRES_BY_FDID_STATE, (fd.fdid, fd.state))
    results = dictfetchall(cursor)
    old_score = fd.dist_model_score
    counts = dict(object_of_origin=0, room_of_origin=0, floor_of_origin=0, building_of_origin=0, beyond=0)

    for result in results:
        if result['fire_sprd'] == '1':
            counts['object_of_origin'] += result['count']

        if result['fire_sprd'] == '2':
            counts['room_of_origin'] += result['count']

        if result['fire_sprd'] == '3':
            counts['floor_of_origin'] += result['count']

        if result['fire_sprd'] == '4':
            counts['building_of_origin'] += result['count']

        if result['fire_sprd'] == '5':
            counts['beyond'] += result['count']

    ahs_building_size = ahs_building_areas(fd.fdid, fd.state)

    if ahs_building_size is not None:
        counts['building_area_draw'] = ahs_building_size

    response_times = response_time_distributions.get('{0}-{1}'.format(fd.fdid, fd.state))

    if response_times:
        counts['arrival_time_draw'] = LogNormalDraw(*response_times, multiplier=60)

    try:
        dist = DIST(floor_extent=False, **counts)
        fd.dist_model_score = dist.gibbs_sample()

    except (NotEnoughRecords, ZeroDivisionError):
        fd.dist_model_score = None

    print 'updating fdid: {2} from: {0} to {1}.'.format(old_score, fd.dist_model_score, fd.id)

    if dry_run:
        return

    fd.save()


@app.task(queue='update')
def update_nfirs_counts(id, year=None):
    """
    Queries the NFIRS database for statistics.
    """

    if not id:
        return

    try:
        fd = FireDepartment.objects.get(id=id)
        cursor = connections['nfirs'].cursor()

    except (FireDepartment.DoesNotExist, ConnectionDoesNotExist):
        return

    years = {}
    if not year:
        # get a list of years populated in the NFIRS database
        years_query = "select distinct(extract(year from inc_date)) as year from buildingfires;"
        cursor.execute(years_query)
        # default years to None
        map(years.setdefault, [int(n[0]) for n in cursor.fetchall()])
    else:
        years[year] = None

    queries = (
        ('civilian_casualties', "select extract(year from inc_date) as year, count(*) from civiliancasualty where"
            " fdid=%s and state=%s and extract(year from inc_date) in %s group by year order by year desc;", (fd.fdid, fd.state, tuple(years.keys()))),
        ('residential_structure_fires', "select extract(year from inc_date) as year, count(*) from buildingfires"
                                        " where fdid=%s and state=%s and extract(year from inc_date) in %s group by year order by year desc;",
         (fd.fdid, fd.state, tuple(years.keys()))),
        ('firefighter_casualties', "select extract(year from inc_date) as year, count(*) from ffcasualty where"
            " fdid=%s and state=%s and extract(year from inc_date) in %s group by year order by year desc;", (fd.fdid, fd.state, tuple(years.keys()))),
    )

    for statistic, query, params in queries:
        counts = years.copy()
        cursor.execute(query, params)

        for year, count in cursor.fetchall():
            counts[year] = count

        for year, count in counts.items():
            nfirs.objects.update_or_create(year=year, defaults={'count': count}, fire_department=fd, metric=statistic)


@app.task(queue='update')
def create_quartile_views_task():
    """
    Updates the Quartile Materialized Views.
    """
    return create_quartile_views(None)


@app.task(queue='update')
def update_heatmap_file(state, fd_id, id):
    cursor = connections['nfirs'].cursor()

    sql = """
       \COPY (select alarm, a.inc_type, alarms,ff_death, oth_death, ST_X(geom) as x, st_y(geom) as y
       from buildingfires a
       left join incidentaddress b using (state, inc_date, exp_no, fdid, inc_no)
       where state=%s and fdid=%s)
       to PROGRAM 'aws s3 cp - s3://firecares-pipeline/heatmaps/%s-building-fires.csv --acl=\"public-read\"' DELIMITER ',' CSV HEADER;
    """
    cmd = cursor.mogrify(sql, (state, fd_id, id))
    cursor.execute(cmd)
