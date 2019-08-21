"""Geodesy-related utility functions."""


from osgeo import gdal
import numpy as np
import pickle
import os

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
    return pyproj.transform(inProj, outProj, lon, lat, height)
    

def lla2ecef(lat, lon, height):
    import pyproj
    ecef = pyproj.Proj(proj='geocent')
    lla = pyproj.Proj(proj='latlong')

    return pyproj.transform(lla, ecef, lon, lat, height)


def ecef2lla(x, y, z):
    import pyproj
    ecef = pyproj.Proj(proj='geocent')
    lla = pyproj.Proj(proj='latlong')
    lon, lat, height = pyproj.transform(ecef, lla, x, y, z)
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
    return pyproj.transform(lla, lambert, lat, lon, height)


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


def gdal_open(fname, returnProj = False):
    if os.path.exists(fname + '.vrt'):
        fname = fname + '.vrt'
    try:
        ds = gdal.Open(fname, gdal.GA_ReadOnly)
    except:
        raise RuntimeError('File {} could not be opened'.format(fname))
    proj = ds.GetProjection()

    val = []
    for band in range(ds.RasterCount):
        b = ds.GetRasterBand(band + 1) # gdal counts from 1, not 0
        d = b.ReadAsArray()
        try:
            ndv = b.GetNoDataValue()
            d[d==ndv]=np.nan
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
        return data, proj


def pickle_load(f):
    with open(f, 'rb') as fil:
        return pickle.load(fil)

def pickle_dump(o, f):
    with open(f, 'wb') as fil:
        pickle.dump(o, fil)


def writeArrayToRaster(array, filename, noDataValue = 0, fmt = 'ENVI', proj = None, gt = None):
    # write a numpy array to a GDAL-readable raster
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


def writeArrayToFile(lats, lons, array, filename, noDataValue = -9999):
    '''
    Write a single-dim array of values to a file
    '''
    array[np.isnan(array)] = noDataValue
    with open(filename, 'w') as f:
       f.write('Lat,Lon,DEM_hgt_m\n')
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
    xlim, ylim, zlim  = np.shape(a)
    for x in range(xlim):
        for y in range(ylim):
            for z in range(zlim):
                val = a[x][y][z]
                if not np.isnan(val):
                    out[x][y] = val
                    break
    return out


def sind(x):
    """Return the sine of x when x is in degrees."""
    return np.sin(np.radians(x))


def cosd(x):
    """Return the cosine of x when x is in degrees."""
    return np.cos(np.radians(x))


def tand(x):
    """Return degree tangent."""
    return np.tan(np.radians(x))


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
    #TODO: verify these constants. In particular why is the reference g different from self._g0?
    return 9.80616*(1 - 0.002637*cosd(2*lats) + 0.0000059*(cosd(2*lats))**2)

def _get_Re(lats):
    '''
    Returns the ellipsoid as a fcn of latitude
    '''
    #TODO: verify constants, add to base class constants? 
    Rmax = 6378137
    Rmin = 6356752
    return np.sqrt(1/(((cosd(lats)**2)/Rmax**2) + ((sind(lats)**2)/Rmin**2)))


def _geo_to_ht(lats, hts, g0 = 9.80556):
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
    return np.concatenate((new_var[:,:,np.newaxis], invar), axis =2)


def testArr(arr, thresh, ttype):
    '''
    Helper function for checking heights
    '''
    if ttype=='g':
        test = np.all(arr>thresh)
    elif ttype =='l':
        test = np.all(arr<thresh)
    else:
        raise RuntimeError('testArr: bad type')

    return test

def getMaxModelLevel(arr3D, thresh, ttype = 'l'):
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


def makeDelayFileNames(time, los,outformat, weather_model_name, out):
    '''
    return names for the wet and hydrostatic delays
    '''
    str1 = time.isoformat() + "_" if time is not None else ""
    str2 = "z" if los is None else "s" 
    str3 = 'td.{}'.format(outformat)
    hydroname, wetname = (
        '{}_{}_'.format(weather_model_name, dtyp) + str1 + str2 + str3
        for dtyp in ('hydro', 'wet'))

    hydro_file_name = os.path.join(out, hydroname)
    wet_file_name = os.path.join(out, wetname)
    return wet_file_name, hydro_file_name


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

    mkdir('geom')

    writeArrayToRaster(lons, os.path.join(out, 'geom', lonFileName))
    writeArrayToRaster(lats, os.path.join(out, 'geom', latFileName))

    return latFileName, lonFileName


def checkShapes(los, lats, lons, hgts):
    '''
    Make sure that by the time the code reaches here, we have a
    consistent set of line-of-sight and position data. 
    '''
    from RAiDER.constants import Zenith
    test1 = hgts.shape == lats.shape == lons.shape
    try:
        test2 = los.shape[:-1] != hts.shape
    except:
        test2 = los is not Zenith

    if not test1 or test2:
        raise ValueError(
         'I need lats, lons, heights, and los to all be the same shape. ' +
         'lats had shape {}, lons had shape {}, '.format(lats.shape, lons.shape)+
         'heights had shape {}, and los was not Zenith'.format(hts.shape))


def checkLOS(los, raytrace, Npts):
    '''
    Check that los is either: 
       (1) Zenith,
       (2) a set of scalar values of the same size as the number 
           of points, which represent the projection value), or
       (3) a set of vectors, same number as the number of points. 
     '''
    from RAiDER.constants import Zenith
    # los can either be a bunch of vectors or a bunch of scalars. If
    # raytrace, then it's vectors, otherwise scalars. (Or it's Zenith)
    if los is not Zenith:
        if raytrace:
            los = los.reshape(-1, 3)
        else:
            los = los.flatten()

    if los is not Zenith and los.shape[0] != Npts:
       raise RuntimeError('Found {} line-of-sight values and only {} points'
                           .format(los.shape[0], Npts))
    return los



def readLLFromStationFile(fname):
    '''
    Helper fcn for checking argument compatibility
    '''
    try:
       import pandas as pd
       stats = pd.read_csv(fname)
       return stats['Lat'].values,stats['Lon'].values
    except:
       lats, lons = [], []
       with open(fname, 'r') as f:
          for i, line in enumerate(f): 
              if i == 0:
                 continue
              lat, lon = [float(f) for f in line.split(',')[1:3]]
              lats.append(lat)
              lons.append(lon)
       return lats, lons

       
def mangle_model_to_module(model_name):
    """Turn an arbitrary string into a module name.

    Takes as input a model name, which hopefully looks like ERA-I, and
    converts it to a module name, which will look like erai. I doesn't
    always produce a valid module name, but that's not the goal. The
    goal is just to handle common cases.
    """
    return 'models.' + model_name.lower().replace('-', '')


def gdal_trans(f1, f2, fmt = 'VRT'):
    '''
    translate a file from one location to another using GDAL
    '''
    ds1 = gdal.Open(f1)
    if ds1 is None:
        raise RuntimeError('Could not open the file {}'.format(f1))
    ds2 = gdal.Translate(f2, ds1, format = fmt)
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


def getExtent(lats, lons=None):
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
