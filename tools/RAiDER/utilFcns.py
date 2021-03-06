"""Geodesy-related utility functions."""


import os
import pickle

import numpy as np
from osgeo import gdal

gdal.UseExceptions()


# Top of the troposphere
zref = 15000


def sind(x):
    """Return the sine of x when x is in degrees."""
    return np.sin(np.radians(x))


def cosd(x):
    """Return the cosine of x when x is in degrees."""
    return np.cos(np.radians(x))


def tand(x):
    """Return degree tangent."""
    return np.tan(np.radians(x))


def reproject(inlat, inlon, inhgt, inProj, outProj):
    '''
    reproject a set of lat/lon/hgts to a new coordinate system
    '''
    import pyproj
    return pyproj.transform(inProj, outProj, inlon, inlat, inhgt, always_xy=True)


def lla2ecef(lat, lon, height):
    import pyproj
    ecef = pyproj.Proj(proj='geocent')
    lla = pyproj.Proj(proj='latlong')

    return pyproj.transform(lla, ecef, lon, lat, height, always_xy=True)


def ecef2lla(x, y, z):
    import pyproj
    ecef = pyproj.Proj(proj='geocent')
    lla = pyproj.Proj(proj='latlong')
    lon, lat, height = pyproj.transform(ecef, lla, x, y, z, always_xy=True)
    return lat, lon, height


def enu2ecef(east, north, up, lat0, lon0, h0):
    """Return ecef from enu coordinates."""
    # I'm looking at
    # https://github.com/scivision/pymap3d/blob/master/pymap3d/__init__.py
    x0, y0, z0 = lla2ecef(lat0, lon0, h0)

    t = cosd(lat0) * up - sind(lat0) * north
    w = sind(lat0) * up + cosd(lat0) * north

    u = cosd(lon0) * t - sind(lon0) * east
    v = sind(lon0) * t + cosd(lon0) * east

    my_ecef = np.stack((x0 + u, y0 + v, z0 + w))

    return my_ecef


def lla2lambert(lat, lon, height=None):
    import pyproj
    lla = pyproj.Proj(proj='latlong')
    lambert = pyproj.Proj(
            '+proj=lcc +lat_1=30.0 +lat_2=60.0 +lat_0=18.500015 +lon_0=-100.2 '
            '+a=6370 +b=6370 +towgs84=0,0,0 +no_defs')

    if height is None:
        return lla(lat, lon, errcheck=True)
    return pyproj.transform(lla, lambert, lat, lon, height, always_xy=True)


def state_to_los(t, x, y, z, vx, vy, vz, lats, lons, heights):
    import Geo2rdr

    real_shape = lats.shape
    lats = lats.flatten()
    lons = lons.flatten()
    heights = heights.flatten()

    geo2rdr_obj = Geo2rdr.PyGeo2rdr()
    geo2rdr_obj.set_orbit(t, x, y, z, vx, vy, vz)

    loss = np.zeros((3, len(lats)))
    slant_ranges = np.zeros_like(lats)

    for i, (lat, lon, height) in enumerate(zip(lats, lons, heights)):
        height_array = np.array(((height,),))

        # Geo2rdr is picky about the type of height
        height_array = height_array.astype(np.double)

        geo2rdr_obj.set_geo_coordinate(np.radians(lon),
                                       np.radians(lat),
                                       1, 1,
                                       height_array)
        # compute the radar coordinate for each geo coordinate
        geo2rdr_obj.geo2rdr()

        # get back the line of sight unit vector
        los_x, los_y, los_z = geo2rdr_obj.get_los()
        loss[:, i] = los_x, los_y, los_z

        # get back the slant ranges
        slant_range = geo2rdr_obj.get_slant_range()
        slant_ranges[i] = slant_range

    los = loss * slant_ranges

    # Have to think about traversal order here. It's easy, though, since
    # in both orders xs come first, followed by all ys, followed by all
    # zs.
    return los.reshape((3,) + real_shape)


def toXYZ(lats, lons, hts):
    """Convert lat, lon, geopotential height to x, y, z in ECEF."""
    # Convert geopotential to geometric height. This comes straight from
    # TRAIN
    g0 = 9.80665
    # Map of g with latitude (I'm skeptical of this equation)
    g = 9.80616*(1 - 0.002637*cosd(2*lats) + 0.0000059*(cosd(2*lats))**2)
    Rmax = 6378137
    Rmin = 6356752
    Re = np.sqrt(1/(((cosd(lats)**2)/Rmax**2) + ((sind(lats)**2)/Rmin**2)))

    # Calculate Geometric Height, h
    h = (hts*Re)/(g/g0*Re - hts)
    return lla2ecef(lats, lons, h)


def big_and(*args):
    result = args[0]
    for a in args[1:]:
        result = np.logical_and(result, a)
    return result


def gdal_open(fname, returnProj=False):
    if os.path.exists(fname + '.vrt'):
        fname = fname + '.vrt'
    try:
        ds = gdal.Open(fname, gdal.GA_ReadOnly)
    except:
        raise RuntimeError('File {} could not be opened'.format(fname))
    proj = ds.GetProjection()
    gt = ds.GetGeoTransform()

    val = []
    for band in range(ds.RasterCount):
        b = ds.GetRasterBand(band + 1)  # gdal counts from 1, not 0
        d = b.ReadAsArray()
        try:
            ndv = b.GetNoDataValue()
            d[d == ndv] = np.nan
        except:
            print('NoDataValue attempt failed*******')
            pass
        val.append(d)
        b = None
    ds = None

    if len(val) > 1:
        data = np.stack(val)
    else:
        data = val[0]

    if not returnProj:
        return data
    else:
        return data, proj, gt


def pickle_load(f):
    with open(f, 'rb') as fil:
        return pickle.load(fil)


def pickle_dump(o, f):
    with open(f, 'wb') as fil:
        pickle.dump(o, fil)


def writeResultsToHDF5(lats, lons, hgts, wet, hydro, filename, delayType=None):
    '''
    write a 1-D array to a NETCDF5 file
    '''
    if delayType is None:
        delayType = "Zenith"

    import h5py
    with h5py.File(filename, 'w') as f:
        f['lat'] = lats
        f['lon'] = lons
        f['hgts'] = hgts
        f['wetDelay'] = wet
        f['hydroDelay'] = hydro
        f['wetDelayUnit'] = "m"
        f['hydroDelayUnit'] = "m"
        f['hgtsUnit'] = "m"
        f.attrs['DelayType'] = delayType

    print('Finished writing data to {}'.format(filename))


def writeArrayToRaster(array, filename, noDataValue=0., fmt='ENVI', proj=None, gt=None):
    '''
    write a numpy array to a GDAL-readable raster
    '''
    import gdal
    import numpy as np
    array_shp = np.shape(array)
    dType = array.dtype
    if 'complex' in str(dType):
        dType = gdal.GDT_CFloat32
    elif 'float' in str(dType):
        dType = gdal.GDT_Float32
    else:
        dType = gdal.GDT_Byte

    driver = gdal.GetDriverByName(fmt)
    ds = driver.Create(filename, array_shp[1], array_shp[0],  1, dType)
    if proj is not None:
        ds.SetProjection(proj)
    if gt is not None:
        ds.SetGeoTransform(gt)
    b1 = ds.GetRasterBand(1)
    b1.WriteArray(array)
    b1.SetNoDataValue(noDataValue)
    ds = None
    b1 = None


def writeArrayToFile(lats, lons, array, filename, noDataValue=-9999):
    '''
    Write a single-dim array of values to a file
    '''
    array[np.isnan(array)] = noDataValue
    with open(filename, 'w') as f:
        f.write('Lat,Lon,Hgt_m\n')
        for l, L, a in zip(lats, lons, array):
            f.write('{},{},{}\n'.format(l, L, a))


def round_date(date, precision):
    import datetime
    # First try rounding up
    # Timedelta since the beginning of time
    datedelta = datetime.datetime.min - date
    # Round that timedelta to the specified precision
    rem = datedelta % precision
    # Add back to get date rounded up
    round_up = date + rem

    # Next try rounding down
    datedelta = date - datetime.datetime.min
    rem = datedelta % precision
    round_down = date - rem

    # It's not the most efficient to calculate both and then choose, but
    # it's clear, and performance isn't critical here.
    up_diff = round_up - date
    down_diff = date - round_down

    return round_up if up_diff < down_diff else round_down


def _least_nonzero(a):
    """Fill in a flat array with the lowest nonzero value.

    Useful for interpolation below the bottom of the weather model.
    """
    out = np.full(a.shape[:2], np.nan)
    xlim, ylim, zlim = np.shape(a)
    for x in range(xlim):
        for y in range(ylim):
            for z in range(zlim):
                val = a[x][y][z]
                if not np.isnan(val):
                    out[x][y] = val
                    break
    return out


def robmin(a):
    '''
    Get the minimum of an array, accounting for empty lists
    '''
    from numpy import nanmin as min
    try:
        return min(a)
    except ValueError:
        return 'N/A'


def robmax(a):
    '''
    Get the minimum of an array, accounting for empty lists
    '''
    from numpy import nanmax as max
    try:
        return max(a)
    except ValueError:
        return 'N/A'


def _get_g_ll(lats):
    '''
    Compute the variation in gravity constant with latitude
    '''
    # TODO: verify these constants. In particular why is the reference g different from self._g0?
    return 9.80616*(1 - 0.002637*cosd(2*lats) + 0.0000059*(cosd(2*lats))**2)


def _get_Re(lats):
    '''
    Returns the ellipsoid as a fcn of latitude
    '''
    # TODO: verify constants, add to base class constants?
    Rmax = 6378137
    Rmin = 6356752
    return np.sqrt(1/(((cosd(lats)**2)/Rmax**2) + ((sind(lats)**2)/Rmin**2)))


def _geo_to_ht(lats, hts, g0=9.80556):
    """Convert geopotential height to altitude."""
    # Convert geopotential to geometric height. This comes straight from
    # TRAIN
    # Map of g with latitude (I'm skeptical of this equation - Ray)
    g_ll = _get_g_ll(lats)
    Re = _get_Re(lats)

    # Calculate Geometric Height, h
    h = (hts*Re)/(g_ll/g0*Re - hts)

    return h


def padLower(invar):
    '''
    add a layer of data below the lowest current z-level at height zmin
    '''
    new_var = _least_nonzero(invar)
    return np.concatenate((new_var[:, :, np.newaxis], invar), axis=2)


def testArr(arr, thresh, ttype):
    '''
    Helper function for checking heights
    '''
    if ttype == 'g':
        test = np.all(arr > thresh)
    elif ttype == 'l':
        test = np.all(arr < thresh)
    else:
        raise RuntimeError('testArr: bad type')

    return test


def getMaxModelLevel(arr3D, thresh, ttype='l'):
    '''
    Returns the model level number to keep
    '''
    for ind, level in enumerate(arr3D.T):
        if testArr(level, thresh, ttype):
            return ind
    return ind


def Chunk(iterable, n):
    """ Split iterable into ``n`` iterables of similar size

    Examples::
        >>> l = [1, 2, 3, 4]
        >>> list(chunked(l, 4))
        [[1], [2], [3], [4]]

        >>> l = [1, 2, 3]
        >>> list(chunked(l, 4))
        [[1], [2], [3], []]

        >>> l = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        >>> list(chunked(l, 4))
        [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10]]

    """
    import math
    chunksize = int(math.ceil(len(iterable) / n))
    return (iterable[i * chunksize:i * chunksize + chunksize]
            for i in range(n))


def makeDelayFileNames(time, los, outformat, weather_model_name, out):
    '''
    return names for the wet and hydrostatic delays.

    # Examples:
    >>> makeDelayFileNames(time(0, 0, 0), None, "h5", "model_name", "some_dir")
    ('some_dir/model_name_wet_00_00_00_ztd.h5', 'some_dir/model_name_hydro_00_00_00_ztd.h5')
    >>> makeDelayFileNames(None, None, "h5", "model_name", "some_dir")
    ('some_dir/model_name_wet_ztd.h5', 'some_dir/model_name_hydro_ztd.h5')
    '''
    format_string = "{model_name}_{{}}_{time}{los}.{ext}".format(
        model_name=weather_model_name,
        time=time.strftime("%H_%M_%S_") if time is not None else "",
        los="ztd" if los is None else "std",
        ext=outformat
    )
    hydroname, wetname = (
        format_string.format(dtyp) for dtyp in ('hydro', 'wet')
    )

    hydro_file_name = os.path.join(out, hydroname)
    wet_file_name = os.path.join(out, wetname)
    return wet_file_name, hydro_file_name


def make_weather_model_filename(name, time, ll_bounds):
    return '{}_{}_{}N_{}N_{}E_{}E.h5'.format(
        name, time.strftime("%Y-%m-%dT%H_%M_%S"), *ll_bounds
    )


def mkdir(dirName):
    try:
        os.mkdir(dirName)
    except FileExistsError:
        pass


def writeLL(time, lats, lons, llProj, weather_model_name, out):
    '''
    If the weather model grid nodes are used, write the lat/lon values
    out to a file
    '''
    from datetime import datetime as dt
    lonFileName = '{}_Lon_{}.dat'.format(weather_model_name,
                                         dt.strftime(time, '%Y_%m_%d_T%H_%M_%S'))
    latFileName = '{}_Lat_{}.dat'.format(weather_model_name,
                                         dt.strftime(time, '%Y_%m_%d_T%H_%M_%S'))

    os.makedirs(os.path.abspath('geom'), exist_ok=True)

    writeArrayToRaster(lons, os.path.join(out, 'geom', lonFileName))
    writeArrayToRaster(lats, os.path.join(out, 'geom', latFileName))

    return latFileName, lonFileName


def checkShapes(los, lats, lons, hts):
    '''
    Make sure that by the time the code reaches here, we have a
    consistent set of line-of-sight and position data.
    '''
    from RAiDER.constants import Zenith
    if los is None:
        los = Zenith
    test1 = hts.shape == lats.shape == lons.shape
    try:
        test2 = los.shape[:-1] == hts.shape
    except AttributeError:
        test2 = los is Zenith

    if not test1 and test2:
        raise ValueError(
         'I need lats, lons, heights, and los to all be the same shape. ' +
         'lats had shape {}, lons had shape {}, '.format(lats.shape, lons.shape) +
         'heights had shape {}, and los was not Zenith'.format(hts.shape))


def checkLOS(los, Npts):
    '''
    Check that los is either:
       (1) Zenith,
       (2) a set of scalar values of the same size as the number
           of points, which represent the projection value), or
       (3) a set of vectors, same number as the number of points.
     '''
    from RAiDER.constants import Zenith
    # los is a bunch of vectors or Zenith
    if los is not Zenith:
        los = los.reshape(-1, 3)

    if los is not Zenith and los.shape[0] != Npts:
        raise RuntimeError('Found {} line-of-sight values and only {} points'
                           .format(los.shape[0], Npts))
    return los


def modelName2Module(model_name):
    """Turn an arbitrary string into a module name.
    Takes as input a model name, which hopefully looks like ERA-I, and
    converts it to a module name, which will look like erai. I doesn't
    always produce a valid module name, but that's not the goal. The
    goal is just to handle common cases.
    Inputs:
       model_name  - Name of an allowed weather model (e.g., 'era-5')
    Outputs:
       module_name - Name of the module
       wmObject    - callable, weather model object
    """
    import importlib
    module_name = 'RAiDER.models.' + model_name.lower().replace('-', '')
    model_module = importlib.import_module(module_name)
    wmObject = getattr(model_module, model_name.upper().replace('-', ''))
    return module_name, wmObject


def gdal_trans(f1, f2, fmt='VRT'):
    '''
    translate a file from one location to another using GDAL
    '''
    ds1 = gdal.Open(f1)
    if ds1 is None:
        raise RuntimeError('Could not open the file {}'.format(f1))
    ds2 = gdal.Translate(f2, ds1, format=fmt)
    if ds2 is None:
        raise RuntimeError('Could not translate the file {} to {}'.format(f1, f2))
    ds1 = None
    ds2 = None


def isOutside(extent1, extent2):
    '''
    Determine whether any of extent1  lies outside extent2
    extent1/2 should be a list containing [lower_lat, upper_lat, left_lon, right_lon]
    '''
    t1 = extent1[0] < extent2[0]
    t2 = extent1[1] > extent2[1]
    t3 = extent1[2] < extent2[2]
    t4 = extent1[3] > extent2[3]
    if np.any([t1, t2, t3, t4]):
        return True
    return False


def getExtent(lats, lons=None, shrink=1):
    '''
    get the bounding box around a set of lats/lons
    '''
    if lons is None:
        ds   = gdal.Open(lats, gdal.GA_ReadOnly)
        trans    = ds.GetGeoTransform()
        # W E S N
        extent   = [trans[0], trans[0] + ds.RasterXSize * trans[1],
                    trans[3] + ds.RasterYSize*trans[5], trans[3]]
        if shrink is not None:
            delW, delE, delS, delN = shrink
            extent = [extent[0] + delW, extent[1] - delE, extent[2] + delS, extent[3] - delN]
        del ds
        return extent

    else:
        return [np.nanmin(lats), np.nanmax(lats), np.nanmin(lons), np.nanmax(lons)]


def setLLds(infile, latfile, lonfile):
    '''
    Use a lat/lon file to set the x/y coordinates of infile
    '''
    from osgeo import gdal, osr
    ds = gdal.Open(infile, gdal.GA_ReadOnly)
    ds.SetMetadata({'X_DATASET': os.path.abspath(latfile), 'X_BAND': '1',
                    'Y_DATASET': os.path.abspath(lonfile), 'Y_BAND': '1'})

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    del ds


def parallel_apply_along_axis(func1d, axis, arr, *args, **kwargs):
    """
    Like numpy.apply_along_axis(), but takes advantage of multiple
    cores.

    This function and the one below (unpacking_apply_along_axis) were
    copied from
    https://stackoverflow.com/questions/45526700/easy-parallelization-of-numpy-apply-along-axis
    """
    # Effective axis where apply_along_axis() will be applied by each
    # worker (any non-zero axis number would work, so as to allow the use
    # of `np.array_split()`, which is only done on axis 0):
    import multiprocessing as mp
    import numpy as np

    effective_axis = 1 if axis == 0 else axis
    if effective_axis != axis:
        arr = arr.swapaxes(axis, effective_axis)

    # Chunks for the mapping (only a few chunks):
    Nchunks = mp.cpu_count()*3//4
    sub_arrs = np.array_split(arr, Nchunks)
    chunks = [(func1d, effective_axis, sub_arr, args, kwargs)
              for sub_arr in sub_arrs]

    with mp.Pool() as pool:
        individual_results = pool.map(unpacking_apply_along_axis, chunks)

    conc_results = np.concatenate(individual_results)
    ordered_results = conc_results.swapaxes(effective_axis, axis)

    return ordered_results


def unpacking_apply_along_axis(tup):
    """
    Like numpy.apply_along_axis(), but and with arguments in a tuple
    instead.

    This function is useful with multiprocessing.Pool().map(): (1)
    map() only handles functions that take a single argument, and (2)
    this function can generally be imported from a module, as required
    by map().
    """
    import numpy as np
    func1d, axis, arr, arg, kwarg = tup
    results = np.apply_along_axis(func1d, axis, arr, *arg, **kwarg)
    return results


def read_hgt_file(filename):
    '''
    Read height data from a comma-delimited file
    '''
    import pandas as pd
    data = pd.read_csv(filename)
    hgts = data['Hgt_m'].values
    return hgts


def parse_date(s):
    """
    Parse a date from a string in pseudo-ISO 8601 format.
    """
    import datetime
    import itertools
    year_formats = (
        '%Y-%m-%d',
        '%Y%m%d'
    )
    date = None
    for yf in year_formats:
        try:
            date = datetime.datetime.strptime(s, yf)
        except ValueError:
            continue

    if date is None:
        raise ValueError(
            'Unable to coerce {} to a date. Try %Y-%m-%d'.format(s))

    return date


def parse_time(t):
    '''
    Parse an input time (required to be ISO 8601)
    '''
    import datetime
    import itertools
    time_formats = (
        '',
        'T%H:%M:%S.%f',
        'T%H%M%S.%f',
        '%H%M%S.%f',
        'T%H:%M:%S',
        '%H:%M:%S',
        'T%H%M%S',
        '%H%M%S',
        'T%H:%M',
        'T%H%M',
        '%H:%M',
        'T%H',
    )
    timezone_formats = (
        '',
        'Z',
        '%z',
    )
    all_formats = map(
        ''.join,
        itertools.product(time_formats, timezone_formats))

    time = None
    for tf in all_formats:
        try:
            time = datetime.datetime.strptime(t, tf) - datetime.datetime(1900, 1, 1)
        except ValueError:
            continue

    if time is None:
        raise ValueError(
            'Unable to coerce {} to a time. Try T%H:%M:%S'.format(t))

    return time


def writeDelays(flag, wetDelay, hydroDelay, lats, lons,
                wetFilename, hydroFilename=None, zlevels=None, delayType=None,
                outformat=None, proj=None, gt=None, ndv=0.):
    '''
    Write the delay numpy arrays to files in the format specified
    '''

    # Need to consistently handle noDataValues
    wetDelay[np.isnan(wetDelay)] = ndv
    hydroDelay[np.isnan(hydroDelay)] = ndv

    # Do different things, depending on the type of input
    if flag == 'station_file':
        import pandas as pd
        df = pd.read_csv(wetFilename)

        # quick check for consistency
        assert(np.all(np.abs(lats - df['Lat']) < 0.01))

        df['wetDelay'] = wetDelay
        df['hydroDelay'] = hydroDelay
        df['totalDelay'] = wetDelay + hydroDelay
        df.to_csv(wetFilename, index=False)

    elif outformat == 'hdf5':
        writeResultsToHDF5(lats, lons, zlevels, wetDelay, hydroDelay, wetFilename, delayType=delayType)
    else:
        writeArrayToRaster(wetDelay, wetFilename, noDataValue=ndv,
                           fmt=outformat, proj=proj, gt=gt)
        writeArrayToRaster(hydroDelay, hydroFilename, noDataValue=ndv,
                           fmt=outformat, proj=proj, gt=gt)


def getTimeFromFile(filename):
    '''
    Parse a filename to get a date-time
    '''
    import datetime
    import re
    fmt = '%Y_%m_%d_T%H_%M_%S'
    p = re.compile(r'\d{4}_\d{2}_\d{2}_T\d{2}_\d{2}_\d{2}')
    try:
        out = p.search(filename).group()
        return datetime.datetime.strptime(out, fmt)
    except:
        raise RuntimeError('File {} is not named by datetime, you must pass a time to '.format(filename))


def writePnts2HDF5(lats, lons, hgts, los, outName='testx.h5', chunkSize=None):
    '''
    Write query points to an HDF5 file for storage and access
    '''
    import datetime
    import h5py
    import os
    from osgeo import osr

    from RAiDER.utilFcns import checkLOS

    epsg = 4326
    projname = 'projection'

    checkLOS(los, np.prod(lats.shape))
    in_shape = lats.shape

    # create directory if needed
    os.makedirs(os.path.abspath(os.path.dirname(outName)), exist_ok=True)

    with h5py.File(outName, 'w') as f:
    # with h5py.File(outName, 'w', chunk_cache_mem_size=1024**2*4000) as f:
        f.attrs['Conventions'] = np.string_("CF-1.8")

        if chunkSize is None:
            x = f.create_dataset('lon', data=lons.astype(np.float64), chunks=True)
        else:
            x = f.create_dataset('lon', data=lons.astype(np.float64), chunks=chunkSize)

        y = f.create_dataset('lat', data=lats.astype(np.float64), chunks=x.chunks)
        z = f.create_dataset('hgt', data=hgts.astype(np.float64), chunks=x.chunks)
        los = f.create_dataset('LOS', data=los.astype(np.float64), chunks=x.chunks + (3,))
        x.attrs['Shape'] = in_shape
        y.attrs['Shape'] = in_shape
        z.attrs['Shape'] = in_shape
        f.attrs['ChunkSize'] = chunkSize

        # CF 1.8 Convention stuff
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        projds = f.create_dataset(projname, (), dtype='i')
        projds[()] = epsg

        # WGS84 ellipsoid
        projds.attrs['semi_major_axis'] = 6378137.0
        projds.attrs['inverse_flattening'] = 298.257223563
        projds.attrs['ellipsoid'] = np.string_("WGS84")
        projds.attrs['epsg_code'] = epsg
        projds.attrs['spatial_ref'] = np.string_(srs.ExportToWkt())

        # Geodetic latitude / longitude
        if epsg == 4326:
            # Set up grid mapping
            projds.attrs['grid_mapping_name'] = np.string_('latitude_longitude')
            projds.attrs['longitude_of_prime_meridian'] = 0.0

            x.attrs['standard_name'] = np.string_("longitude")
            x.attrs['units'] = np.string_("degrees_east")
            y.attrs['standard_name'] = np.string_("latitude")
            y.attrs['units'] = np.string_("degrees_north")
            z.attrs['standard_name'] = np.string_("height")
            z.attrs['units'] = np.string_("m")
        else:
            raise NotImplemented

        start_positions = f.create_dataset('Rays_SP', in_shape + (3,), chunks=los.chunks, dtype='<f8')
        lengths = f.create_dataset('Rays_len',  in_shape, chunks=x.chunks, dtype='<f8')
        scaled_look_vecs = f.create_dataset('Rays_SLV',  in_shape + (3,), chunks=los.chunks, dtype='<f8')

        los.attrs['grid_mapping'] = np.string_(projname)
        start_positions.attrs['grid_mapping'] = np.string_(projname)
        lengths.attrs['grid_mapping'] = np.string_(projname)
        scaled_look_vecs.attrs['grid_mapping'] = np.string_(projname)

        f.attrs['NumRays'] = len(x)


def makePoints1D(max_len, Rays_SP, Rays_SLV, stepSize):
    '''
    Python version of cython code to create the rays needed for ray-tracing
    Inputs:
      max_len: maximum length of the rays
      Rays_SP: 1 x 3 numpy array of the location of the ground pixels in an earth-centered,
               earth-fixed coordinate system
      Rays_SLV: 1 x 3 numpy array of the look vectors pointing from the ground pixel to the sensor
      stepSize: Distance between points along the ray-path
    Output:
      ray: a Nx x Ny x Nz x 3 x Npts array containing the rays tracing a path from the ground pixels, along the
           line-of-sight vectors, up to the maximum length specified.
    '''
    Npts  = int(max_len//stepSize) + [1 if max_len % stepSize != 0. else 0][0]
    ray = np.empty((3, Npts), dtype=np.float64)
    basespace = np.arange(0, max_len, stepSize)  # max_len+stepSize
    for k3 in range(3):
        ray[k3, :] = Rays_SP[k3] + basespace*Rays_SLV[k3]
    return ray

def makePoints3D(max_len, Rays_SP, Rays_SLV, stepSize):
    '''
    Python version of cython code to create the rays needed for ray-tracing
    Inputs:
      max_len: maximum length of the rays
      Rays_SP: Nx x Ny x Nz x 3 numpy array of the location of the ground pixels in an earth-centered,
               earth-fixed coordinate system
      Rays_SLV: Nx x Ny x Nz x 3 numpy array of the look vectors pointing from the ground pixel to the sensor
      stepSize: Distance between points along the ray-path
    Output:
      ray: a Nx x Ny x Nz x 3 x Npts array containing the rays tracing a path from the ground pixels, along the
           line-of-sight vectors, up to the maximum length specified.
    '''
    Npts  = int(max_len//stepSize) + [1 if max_len % stepSize != 0. else 0][0]
    nrow = Rays_SP.shape[0]
    ncol = Rays_SP.shape[1]
    nz = Rays_SP.shape[2]
    ray = np.empty((nrow, ncol, nz, 3, Npts), dtype=np.float64)
    basespace = np.arange(0, max_len, stepSize)  # max_len+stepSize

    for k1 in range(nrow):
        for k2 in range(ncol):
            for k2a in range(nz):
                for k3 in range(3):
                    ray[k1, k2, k2a, k3, :] = Rays_SP[k1, k2, k2a, k3] + basespace*Rays_SLV[k1, k2, k2a, k3]
    return ray
