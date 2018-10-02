"""Compute the delay from a point to the transmitter.

Dry and hydrostatic delays are calculated in separate functions.
Currently we take samples every _STEP meters, which causes either
inaccuracies or inefficiencies, and we no longer can integrate to
infinity.
"""


import demdownload
from osgeo import gdal
import itertools
import losreader
import numpy as np
import os
import os.path
import tempfile
import queue
import threading
import util
import wrf
import reader

gdal.UseExceptions()


# Step in meters to use when integrating
_STEP = 15

# Top of the troposphere
_ZREF = util.zref


class Zenith:
    """Special value indicating a look vector of "zenith"."""
    pass


def _too_high(positions, zref):
    """Find index of first position higher than zref.

    This is useful when we're trying to cut off integration at the top
    of the troposphere. I calculate the list of all points, then use
    this function to compute the first index above the troposphere, then
    I can cut the list down to just the important points.
    """
    positions_ecef = np.moveaxis(positions, -1, 0)
    positions_lla = np.stack(util.ecef2lla(*positions_ecef))
    high_indices = np.where(positions_lla[2] > zref)[0]
    first_high_index = high_indices[0] if high_indices.size else len(positions)
    return first_high_index


def _get_lengths(look_vecs):
    '''
    Returns the lengths of something
    '''
    lengths = np.linalg.norm(look_vecs, axis=-1)
    lengths[~np.isfinite(lengths)] = 0
    return lengths


def _get_steps(lengths):
    '''
    Get the number of integration steps for each path
    '''
    steps = np.array(np.ceil(lengths / _STEP), dtype=np.int64)
    steps[steps < 0] = 0
    return steps


def _common_delay(delay, lats, lons, heights, look_vecs, raytrace):
    """Perform computation common to hydrostatic and wet delay."""
    # Deal with Zenith special value, and non-raytracing method
    if raytrace:
        correction = None
    else:
        correction = 1/util.cosd(look_vecs)
        look_vecs = Zenith
    if look_vecs is Zenith:
        look_vecs = (np.array((util.cosd(lats)*util.cosd(lons),
                               util.cosd(lats)*util.sind(lons),
                               util.sind(lats))).T
                     * (_ZREF - heights)[..., np.newaxis])
    lengths = _get_lengths(look_vecs)
    steps = _get_steps(lengths)
    start_positions = np.array(util.lla2ecef(lats, lons, heights)).T
    scaled_look_vecs = look_vecs / lengths[..., np.newaxis]

    if verbose:
        print('_common_delay: The size of look_vecs is {}'.format(np.shape(look_vecs)))
        print('_common_delay: The number of steps is {}'.format(len(steps)))

    positions_l = list()
    t_points_l = list()
    for i, N in enumerate(steps):
        # Have to handle the case where there are invalid data
        if N==0:
            t_points_l.append(np.empty((0,3)))
            positions_l.append(np.empty((0,3)))
            continue
        else:
            thisspace = np.linspace(0, lengths[i], N)

        position = (start_positions[i]
                    + thisspace[..., np.newaxis]*scaled_look_vecs[i])
        first_high_index = _too_high(position, _ZREF)
        t_points_l.append(thisspace[:first_high_index])
        positions_l.append(position[:first_high_index])

        # Also correct the number of steps
        steps[i] = first_high_index

    if verbose:
        print('_common_delay: Finished steps')

    positions_a = np.concatenate(positions_l)

    if verbose:
        print('_common_delay: starting wet_delay calculation')
    wet_delays = delay(positions_a)

    # Compute starting indices
    indices = np.cumsum(steps)
    # We want the first index to be 0, and the others shifted
    indices = np.roll(indices, 1)
    indices[0] = 0

    if verbose:
        print('_common_delay: starting integration')
    delays = np.zeros(lats.shape[0])
    for i,length in enumerate(steps):
        if length ==0:
            continue
        start = indices[i]
        chunk = wet_delays[start:start + length]
        t_points = t_points_l[i]
        delays[i] = 1e-6 * np.trapz(chunk, t_points)

    if verbose:
        print('_common_delay: Finished integration')

    # Finally apply cosine correction if applicable
    if correction is not None:
        delays *= correction

    return delays


def wet_delay(weather, lats, lons, heights, look_vecs, raytrace=True, verbose = False):
    """Compute wet delay along the look vector."""

    if verbose:
        print('wet_delay: Running _common_delay for weather.wet_delay')

    return _common_delay(weather.wet_delay, lats, lons, heights, look_vecs,
                         raytrace, verbose)


def hydrostatic_delay(weather, lats, lons, heights, look_vecs, raytrace=True, verbose = False):
    """Compute hydrostatic delay along the look vector."""

    if verbose:
        print('hydrostatic_delay: Running _common_delay for weather.hydrostatic_delay')

    return _common_delay(weather.hydrostatic_delay, lats, lons, heights,
                         look_vecs, raytrace, verbose)


def delay_over_area(weather, 
                    lat_min, lat_max, lat_res, 
                    lon_min, lon_max, lon_res, 
                    ht_min, ht_max, ht_res, 
                    los=Zenith, 
                    parallel = True, verbose = False):
    """Calculate (in parallel) the delays over an area."""
    lats = np.arange(lat_min, lat_max, lat_res)
    lons = np.arange(lon_min, lon_max, lon_res)
    hts = np.arange(ht_min, ht_max, ht_res)

    if verbose:
        print('delay_over_area: Size of lats: {}'.format(np.shape(lats)))
        print('delay_over_area: Size of lons: {}'.format(np.shape(lons)))
        print('delay_over_area: Size of hts: {}'.format(np.shape(hts)))

    # It's the cartesian product (thanks StackOverflow)
    llas = np.array(np.meshgrid(lats, lons, hts)).T.reshape(-1, 3)
    if verbose:
        print('delay_over_area: Size of llas: {}'.format(np.shape(llas)))

    if verbose:
        print('delay_over_area: running delay_from_grid')

    return delay_from_grid(weather, llas, los, parallel=parallel, verbose = verbose)


def _parmap(f, i):
    """Execute f on elements of i in parallel."""
    # Queue of jobs
    q = queue.Queue()
    # Space for answers
    answers = list()
    for idx, x in enumerate(i):
        q.put((idx, x))
        answers.append(None)

    def go():
        while True:
            try:
                i, elem = q.get_nowait()
            except queue.Empty:
                break
            answers[i] = f(elem)

    threads = [threading.Thread(target=go) for _ in range(os.cpu_count())]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    return answers


def delay_from_grid(weather, llas, los, parallel=False, raytrace=True, verbose = False):
    """Calculate delay on every point in a list.

    weather is the weather object, llas is a list of lat, lon, ht points
    at which to calculate delay, and los an array of line-of-sight
    vectors at each point. Pass parallel=True if you want to have real
    speed. If raytrace=True, we'll do raytracing, if raytrace=False,
    we'll do projection.
    """

    # Save the shape so we can restore later, but flatten to make it
    # easier to think about
    real_shape = llas.shape[:-1]
    llas = llas.reshape(-1, 3)
    # los can either be a bunch of vectors or a bunch of scalars. If
    # raytrace, then it's vectors, otherwise scalars. (Or it's Zenith)
    if verbose: 
        if los is Zenith:
           print("LOS is Zenith")
        else:
           print('LOS is not Zenith')

    if los is not Zenith:
        if raytrace:
            los = los.reshape(-1, 3)
        else:
            los = los.flatten()

    lats, lons, hts = np.moveaxis(llas, -1, 0)

    if parallel:
        # TODO: Need to specify a specific number of processors to use, not use all open
        num_procs = os.cpu_count()
        if verbose:
            print('{} processors available'.format(num_procs))
        os.nice(10)

        hydro_procs = num_procs // 2
        wet_procs = num_procs - hydro_procs

        if verbose: 
            print('delay_from_grid: using {} processors for hydro delay'.format(hydro_procs))
            print('delay_from_grid: using {} processors for wet delay'.format(wet_procs))

        # Divide up jobs into an appropriate number of pieces
        hindices = np.linspace(0, len(llas), hydro_procs + 1, dtype=int)

        if verbose: 
            print('delay_from_grid: hindices: {}'.format(hindices))

        # Build the jobs
        hjobs = (('hydro', hindices[i], hindices[i + 1], verbose)
                 for i in range(hydro_procs))
        wjobs = (('wet', hindices[i], hindices[i + 1], verbose)
                 for i in range(wet_procs))
        jobs = itertools.chain(hjobs, wjobs)

        # Parallel worker
        def go(job):
            job_type, start, end, verbose = job
            if los is Zenith:
                my_los = Zenith
            else:
                my_los = los[start:end]
            if job_type == 'hydro':
                return hydrostatic_delay(weather, lats[start:end],
                                         lons[start:end], hts[start:end],
                                         my_los, raytrace=raytrace, verbose=verbose)
            if job_type == 'wet':
                return wet_delay(weather, lats[start:end], lons[start:end],
                                 hts[start:end], my_los, raytrace=raytrace, verbose = verbose)
            raise ValueError('Unknown job type {}'.format(job_type))

        # Execute the parallel worker
        result = _parmap(go, jobs)

        # Collect results
        hydro = np.concatenate(result[:hydro_procs])
        wet = np.concatenate(result[hydro_procs:])
    else:
        hydro = hydrostatic_delay(weather, lats, lons, hts, los,
                                  raytrace=raytrace, verbose = verbose)
        wet = wet_delay(weather, lats, lons, hts, los, raytrace=raytrace, verbose = verbose)

    # Restore shape
    hydro, wet = np.stack((hydro, wet)).reshape((2,) + real_shape)

    return hydro, wet


def delay_from_files(weather, lat, lon, ht, parallel=False, los=Zenith,
                     raytrace=True, verbose = False):
    """Read location information from files and calculate delay."""
    lats = util.gdal_open(lat)
    lons = util.gdal_open(lon)
    hts = util.gdal_open(ht)

    if los is not Zenith:
        incidence, heading = util.gdal_open(los)
        if raytrace:
            los = losreader.los_to_lv(
                incidence, heading, lats, lons, hts, _ZREF).reshape(-1, 3)
        else:
            los = incidence.flatten()

    # We need the three to be the same shape so that we know what to
    # reshape hydro and wet to. Plus, them being different sizes
    # indicates a definite user error.
    if not lats.shape == lons.shape == hts.shape:
        raise ValueError(f'lat, lon, and ht should have the same shape, but '
                         'instead lat had shape {lats.shape}, lon had shape '
                         '{lons.shape}, and ht had shape {hts.shape}')

    llas = np.stack((lats.flatten(), lons.flatten(), hts.flatten()), axis=1)
    hydro, wet = delay_from_grid(weather, llas, los,
                                 parallel=parallel, raytrace=raytrace, verbose = verbose)
    hydro, wet = np.stack((hydro, wet)).reshape((2,) + lats.shape)
    return hydro, wet


def _tropo_delay_with_values(los, lats, lons, hts, 
                             weather, zref, 
                             time, 
                             raytrace = True,
                             parallel = True, verbose = False):
    """Calculate troposphere delay from processed command-line arguments."""
    # LOS
    if los is None:
        los = Zenith
    else:
        los = losreader.infer_los(los, lats, lons, hts, zref)

    # We want to test if any shapes are different
    if (not hts.shape == lats.shape == lons.shape
            or los is not Zenith and los.shape[:-1] != hts.shape):
        raise ValueError(
            'I need lats, lons, heights, and los to all be the same shape. '
            f'lats had shape {lats.shape}, lons had shape {lons.shape}, '
            'heights had shape {hts.shape}, and los had shape {los.shape}')

    if verbose: 
        print('_tropo_delay_with_values: called delay_from_grid')

    # Do the calculation
    llas = np.stack((lats, lons, hts), axis=-1)
    hydro, wet = delay_from_grid(weather, llas, los, parallel=parallel,
                                 raytrace=raytrace, verbose = verbose)
    return hydro, wet


def get_weather_and_nodes(model, filename, zmin=None):
    """Look up weather information from a model and file.

    We use the module.load method to load the weather model file, but
    we'll also create a weather model object for it.
    """
    xs, ys, proj, t, q, z, lnsp = model.load(filename)
    return (reader.read_model_level(module, xs, ys, proj, t, q, z, lnsp, zmin),
            xs, ys, proj)


def tropo_delay(los = None, lat = None, lon = None, 
                heights = None, 
                weather = None, 
                zref = 15000, 
                out = None, 
                time = None,
                outformat='ENVI', 
                parallel=True,
                verbose = False):
    """Calculate troposphere delay from command-line arguments.

    We do a little bit of preprocessing, then call
    _tropo_delay_with_values. Then we'll write the output to the output
    file.
    """
    import pyproj

    if out is None:
        out = os.getcwd()

    # Make weather
    weather_type = weather['type']
    weather_files = weather['files']
    weather_fmt = weather['name']

    # For later
    hydroname, wetname = (
        f'{weather_fmt}_{dtyp}_'
        f'{time.isoformat() + "_" if time is not None else ""}'
        f'{"z" if los is None else "s"}td.{outformat}'
        for dtyp in ('hydro', 'wet'))

    hydro_file_name = os.path.join(out, hydroname)
    wet_file_name = os.path.join(out, wetname)

    # set_geo_info should be a list of functions to call on the dataset,
    # and each will do some bit of work
    set_geo_info = list()

    # Lat, lon
    if lat is None:
        # They'll get set later with weather
        lats = lons = None
        latproj = lonproj = None
    else:
        lats, latproj = util.gdal_open(lat, returnProj = True)
        lons, lonproj = util.gdal_open(lon, returnProj = True)

    # set_geo_info should be a list of functions to call on the dataset,
    # and each will do some bit of work
    set_geo_info = list()
    if lat is not None:
        def geo_info(ds):
            ds.SetMetadata({'X_DATASET': os.path.abspath(lat), 'X_BAND': '1',
                            'Y_DATASET': os.path.abspath(lon), 'Y_BAND': '1'})
        set_geo_info.append(geo_info)
    # Is it ever possible that lats and lons will actually have embedded
    # projections?
    if latproj:
        def geo_info(ds):
            ds.SetProjection(latproj)
        set_geo_info.append(geo_info)
    elif lonproj:
        def geo_info(ds):
            ds.SetProjection(lonproj)
        set_geo_info.append(geo_info)

    height_type, height_info = heights
    if verbose:
        print('Type of height: {}'.format(height_type))
        print('Type of weather model: {}'.format(weather_type))
        if weather_files is not None:
            print('{} weather files'.format(len(weather_files)))
        print('Weather format: {}'.format(weather_fmt))

    if weather_type == 'wrf':
        weather = wrf.load(*weather_files)

        # Let lats and lons to weather model nodes if necessary
        if lats is None:
            lats, lons = wrf.wm_nodes(*weather_files)
    else:
        weather_model = weather_type
        if weather_files is None:
            if lats is None:
                raise ValueError(
                    'Unable to infer lats and lons if you also want me to '
                    'download the weather model')
            if verbose:
                f = os.path.join(out, 'weather_model.dat')
                weather_model.fetch(lats, lons, time, f)
                weather = weather_model.load(f)
            else:
                with tempfile.NamedTemporaryFile() as f:
                weather_model.fetch(lats, lons, time, f)
                weather = weather_model.load(f)
        else:
            weather, xs, ys, proj = weather_model.weather_and_nodes(
                weather_files)
            if lats is None:
                def geo_info(ds):
                    ds.SetProjection(str(proj))
                    ds.SetGeoTransform((xs[0], xs[1] - xs[0], 0, ys[0], 0,
                                        ys[1] - ys[0]))
                set_geo_info.append(geo_info)
                lla = pyproj.Proj(proj='latlong')
                xgrid, ygrid = np.meshgrid(xs, ys, indexing='ij')
                lons, lats = pyproj.transform(proj, lla, xgrid, ygrid)

    # Height
    if height_type == 'dem':
        hts = util.gdal_open(height_info)
    elif height_type == 'lvs':
        hts = height_info
    elif height_type == 'download':
        hts = demdownload.download_dem(lats, lons)
    else:
        raise ValueError(f'Unexpected height_type {repr(height_type)}')

    # Pretty different calculation depending on whether they specified a
    # list of heights or just a DEM
    if height_type == 'lvs':
        shape = (len(hts),) + lats.shape
        total_hydro = np.zeros(shape)
        total_wet = np.zeros(shape)
        for i, ht in enumerate(hts):
            hydro, wet = _tropo_delay_with_values(
                los, lats, lons, np.broadcast_to(ht, lats.shape), weather,
                zref, time, parallel=parallel, verbose = verbose)
            total_hydro[i] = hydro
            total_wet[i] = wet

        if outformat == 'hdf5':
            raise NotImplemented
        else:
            drv = gdal.GetDriverByName(outformat)
            hydro_ds = drv.Create(
                hydro_file_name, total_hydro.shape[2],
                total_hydro.shape[1], len(hts), gdal.GDT_Float64)
            for lvl, (hydro, ht) in enumerate(zip(total_hydro, hts), start=1):
                band = hydro_ds.GetRasterBand(lvl)
                band.SetDescription(str(ht))
                band.WriteArray(hydro)
            for f in set_geo_info:
                f(hydro_ds)
            hydro_ds = None

            wet_ds = drv.Create(
                wet_file_name, total_wet.shape[2],
                total_wet.shape[1], len(hts), gdal.GDT_Float64)
            for lvl, (wet, ht) in enumerate(zip(total_wet, hts), start=1):
                band = wet_ds.GetRasterBand(lvl)
                band.SetDescription(str(ht))
                band.WriteArray(wet)
            for f in set_geo_info:
                f(wet_ds)
            wet_ds = None

    else:
        hydro, wet = _tropo_delay_with_values(
            los, lats, lons, hts, weather, zref, time, parallel = parallel, verbose = verbose)

        # Write the output file
        # TODO: maybe support other files than ENVI
        if outformat == 'hdf5':
            raise NotImplemented
        else:
            drv = gdal.GetDriverByName(outformat)
            hydro_ds = drv.Create(
                hydro_file_name, hydro.shape[1], hydro.shape[0],
                1, gdal.GDT_Float64)
            hydro_ds.GetRasterBand(1).WriteArray(hydro)
            for f in set_geo_info:
                f(hydro_ds)
            hydro_ds = None
            wet_ds = drv.Create(
                wet_file_name, wet.shape[1], wet.shape[0], 1,
                gdal.GDT_Float64)
            wet_ds.GetRasterBand(1).WriteArray(wet)
            for f in set_geo_info:
                f(wet_ds)
            wet_ds = None

    return hydro_file_name, wet_file_name
