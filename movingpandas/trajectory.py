# -*- coding: utf-8 -*-

import warnings

from shapely.affinity import translate
from shapely.geometry import Point, LineString
from pandas import DataFrame, to_datetime, Series
from pandas.core.indexes.datetimes import DatetimeIndex
from geopandas import GeoDataFrame
from geopy.distance import geodesic

try:
    from pyproj import CRS
except ImportError:
    # TODO: fiona.crs is deprecated from fiona 2.0, use fiona.CRS instead
    from fiona.crs import from_epsg

from .overlay import clip, intersection, intersects, create_entry_and_exit_points
from .time_range_utils import SpatioTemporalRange
from .geometry_utils import (
    angular_difference,
    azimuth,
    calculate_initial_compass_bearing,
    measure_distance_geodesic,
    measure_distance_euclidean,
    point_gdf_to_linestring,
)
from .unit_utils import (
    UNITS,
    DISTANCE_UNIT_LIST,
    TIME_UNIT_LIST,
    MissingCRSWarning,
    to_unixtime,
    get_conversion,
)
from .trajectory_plotter import _TrajectoryPlotter

warnings.filterwarnings(  # see https://github.com/movingpandas/movingpandas/issues/289
    "ignore", message="CRS not set for some of the concatenation inputs."
)

ACCELERATION_COL_NAME = "acceleration"
ANGULAR_DIFFERENCE_COL_NAME = "angular_difference"
DIRECTION_COL_NAME = "direction"
DISTANCE_COL_NAME = "distance"
SPEED_COL_NAME = "speed"
TIMEDELTA_COL_NAME = "timedelta"
TRAJ_ID_COL_NAME = "traj_id"


class TimeZoneWarning(UserWarning, ValueError):
    pass


class Trajectory:
    def __init__(
        self,
        df,
        traj_id,
        obj_id=None,
        t=None,
        x=None,
        y=None,
        crs="epsg:4326",
        parent=None,
    ):
        """
        Create Trajectory from GeoDataFrame or DataFrame.

        Parameters
        ----------
        df : GeoDataFrame or DataFrame
            GeoDataFrame with point geometry column and timestamp index
        traj_id : any
            Trajectory ID
        obj_id : any
            Moving object ID
        t : string
            Name of the DataFrame column containing the timestamp
        x : string
            Name of the DataFrame column containing the x coordinate
        y : string
            Name of the DataFrame column containing the y coordinate
        crs : string
            CRS of the x/y coordinates
        parent : Trajectory
            Parent trajectory

        Examples
        --------
        Creating a trajectory from scratch:

        >>> import pandas as pd
        >>> import geopandas as gpd
        >>> import movingpandas as mpd
        >>> from fiona.crs import from_epsg
        >>>
        >>> df = pd.DataFrame([
        ...     {'geometry':Point(0,0), 't':datetime(2018,1,1,12,0,0)},
        ...     {'geometry':Point(6,0), 't':datetime(2018,1,1,12,6,0)},
        ...     {'geometry':Point(6,6), 't':datetime(2018,1,1,12,10,0)},
        ...     {'geometry':Point(9,9), 't':datetime(2018,1,1,12,15,0)}
        ... ]).set_index('t')
        >>> gdf = gpd.GeoDataFrame(df, crs=from_epsg(31256))
        >>> traj = mpd.Trajectory(gdf, 1)

        For more examples, see the tutorial notebooks_.

        .. _notebooks: https://mybinder.org/v2/gh/movingpandas/movingpandas/main?filepath=tutorials/1-getting-started.ipynb
        """  # noqa: E501

        if len(df) < 2:
            raise ValueError("The input DataFrame must have at least two rows.")
        if not isinstance(df, GeoDataFrame):
            if x is None or y is None:
                raise ValueError(
                    "The input DataFrame needs to be a GeoDataFrame or x and y columns"
                    "need to be specified."
                )
            df = GeoDataFrame(
                df.drop([x, y], axis=1),
                crs=crs,
                geometry=[Point(xy) for xy in zip(df[x], df[y])],
            )
        if not isinstance(df.index, DatetimeIndex):
            if t is None:
                raise TypeError(
                    "The input DataFrame needs a DatetimeIndex or a timestamp column"
                    "needs to be specified. Use Pandas' set_index() method to create an"
                    "index or specify the timestamp column name."
                )
            df[t] = to_datetime(df[t])
            df = df.set_index(t)

        #  Drop any time zone information, to avoid errors (see issue 303)
        if df.index.tzinfo is not None:
            df = df.tz_localize(None)
            warnings.warn(
                "Time zone information dropped from trajectory. "
                "All dates and times will use local time. "
                "To use UTC or a different time zone, convert and drop "
                "time zone information prior to trajectory creation.",
                category=TimeZoneWarning,
            )

        self.id = traj_id
        self.obj_id = obj_id
        df.sort_index(inplace=True)
        self.df = df[~df.index.duplicated(keep="first")]
        self.crs = df.crs
        self.parent = parent
        if self.crs is not None:
            self.crs_units = self.crs.axis_info[0].unit_name
        else:
            self.crs_units = None
            warnings.warn(
                "Trajectory generated without CRS. Computations will use Euclidean"
                " distances.",
                category=MissingCRSWarning,
            )
            self.is_latlon = False
            return
        try:
            crs = CRS.from_user_input(self.crs)
            self.is_latlon = crs.is_geographic
        except NameError:
            self.is_latlon = self.crs["init"] == from_epsg(4326)["init"]

    def __str__(self):
        try:
            line = self.to_linestring()
        except RuntimeError:
            return "Invalid trajectory!"
        return (
            "Trajectory {id} ({t0} to {tn}) | Size: {n} | Length: {len:.1f}m\n"
            "Bounds: {bbox}\n{wkt}".format(
                id=self.id,
                t0=self.get_start_time(),
                tn=self.get_end_time(),
                n=self.size(),
                wkt=line.wkt[:100],
                bbox=self.get_bbox(),
                len=self.get_length(),
            )
        )

    def __repr__(self):
        return self.__str__()

    def __len__(self):
        return self.get_length()

    def __eq__(self, other):
        # TODO: make bullet proof
        return (
            str(self) == str(other)
            and self.crs == other.crs
            and self.parent == other.parent
        )

    def size(self):
        """
        Returns number of rows in Trajectory.df

        Returns
        -------
        size : int
            Number of rows
        """
        return len(self.df.index)

    def copy(self):
        """
        Return a copy of the trajectory.

        Returns
        -------
        Trajectory
        """
        copied = Trajectory(self.df.copy(), self.id, parent=self.parent)
        return copied

    def plot(self, *args, **kwargs):
        """
        Generate a plot using GeoPandas default plotting (Matplotlib).

        Parameters
        ----------
        args :
            These parameters will be passed to the TrajectoryPlotter
        kwargs :
            These parameters will be passed to the TrajectoryPlotter

        Returns
        -------
        Matplotlib plot

        Examples
        --------
        Plot speed along trajectory (with legend and specified figure size):

        >>> trajectory.plot(column='speed', legend=True, figsize=(9,5))
        """
        return _TrajectoryPlotter(self, *args, **kwargs).plot()

    def hvplot(self, *args, **kwargs):
        """
        Generate an interactive plot using HoloViews.

        The following parameters are set by default:

        geo=True, tiles='OSM', marker_size=200, line_width=2.0

        Parameters
        ----------
        args :
            These parameters will be passed to the TrajectoryPlotter
        kwargs :
            These parameters will be passed to the TrajectoryPlotter

        Returns
        -------
        Holoviews plot

        Examples
        --------
        Plot speed along trajectory (with legend and specified figure size):

        >>> trajectory.hvplot(c='speed', line_width=7.0, width=700, height=400, colorbar=True)
        """  # noqa: E501
        return _TrajectoryPlotter(self, *args, **kwargs).hvplot()

    def is_valid(self):
        """
        Return whether the trajectory meets minimum requirements.

        Returns
        -------
        bool
        """
        if len(self.df) < 2:
            return False
        if not self.get_start_time() < self.get_end_time():
            return False
        return True

    def to_crs(self, crs):
        """
        Returns the trajectory reprojected to the target CRS.

        Parameters
        ----------
        crs : pyproj.CRS
            Target coordinate reference system

        Returns
        -------
        Trajectory

        Examples
        --------
        Reproject a trajectory to EPSG:4088

        >>> from pyproj import CRS
        >>> reprojected = trajectory.to_crs(CRS(4088))
        """
        temp = self.copy()
        temp.crs = crs
        temp.df = temp.df.to_crs(crs)
        if type(crs) == CRS:
            temp.is_latlon = crs.is_geographic
        else:
            temp.is_latlon = crs["init"] == from_epsg(4326)["init"]
        return temp

    def get_speed_column_name(self):
        """
        Return name of the speed column

        Returns
        -------
        string
        """
        if hasattr(self, "speed_col_name"):
            return self.speed_col_name
        else:
            return SPEED_COL_NAME

    def get_distance_column_name(self):
        """
        Return name of the distance column

        Returns
        -------
        string
        """
        if hasattr(self, "distance_col_name"):
            return self.distance_col_name
        else:
            return DISTANCE_COL_NAME

    def get_direction_column_name(self):
        """
        Return name of the direction column

        Returns
        -------
        string
        """
        if hasattr(self, "direction_col_name"):
            return self.direction_col_name
        else:
            return DIRECTION_COL_NAME

    def get_angular_difference_column_name(self):
        """
        Retrun name of the angular difference column

        Returns
        -------
        string
        """
        if hasattr(self, "angular_difference_col_name"):
            return self.angular_difference_col_name
        else:
            return ANGULAR_DIFFERENCE_COL_NAME

    def get_timedelta_column_name(self):
        """
        Return name of the timedelta column

        Returns
        -------
        string
        """
        if hasattr(self, "timedelta_col_name"):
            return self.timedelta_col_name
        else:
            return TIMEDELTA_COL_NAME

    def get_geom_column_name(self):
        """
        Return name of the geometry column

        Returns
        -------
        string
        """
        return self.df.geometry.name

    def to_linestring(self):
        """
        Return trajectory geometry as LineString.

        Returns
        -------
        shapely LineString
        """
        try:
            return point_gdf_to_linestring(self.df, self.get_geom_column_name())
        except RuntimeError:
            raise RuntimeError("Cannot generate LineString")

    def to_linestringm_wkt(self):
        """
        Return the WKT string of the trajectory LineStringM representation.

        Returns
        -------
        string
            WKT of trajectory as LineStringM
        """
        # Shapely only supports x, y, z. Therefore, this is a bit hacky!
        coords = ""
        for index, row in self.df.iterrows():
            pt = row[self.get_geom_column_name()]
            t = to_unixtime(index)
            coords += "{} {} {}, ".format(pt.x, pt.y, t)
        wkt = "LINESTRING M ({})".format(coords[:-2])
        return wkt

    def to_point_gdf(self):
        """
        Return the trajectory's points as GeoDataFrame.

        Returns
        -------
        GeoDataFrame
        """
        return self.df

    def to_line_gdf(self):
        """
        Return the trajectory's line segments as GeoDataFrame.

        Returns
        -------
        GeoDataFrame
        """
        line_gdf = self._to_line_df()
        line_gdf.drop(columns=[self.get_geom_column_name(), "prev_pt"], inplace=True)
        line_gdf.reset_index(drop=True, inplace=True)
        line_gdf.rename(columns={"line": "geometry"}, inplace=True)
        line_gdf.set_geometry("geometry", inplace=True)
        return line_gdf

    def to_traj_gdf(self, wkt=False, agg=False):
        """
        Return a GeoDataFrame with one row containing the trajectory as a
        single LineString.

        Parameters
        ----------
        wkt : bool
            If True, adds WKT column representing the trajectory geometry
        agg : dict
            Adds columns with aggregate values computed from trajectory dataframe
            columns according to specified aggregation mode, using
            pandas.DataFrame.agg(), and shortcuts for "mode" and quantiles
            (e.g. "q5" or "q95")

        Examples
        --------

        >>> traj.to_traj_gdf(agg={"col1": "mode", "col2": ["min", "q95"]})

        Returns
        -------
        GeoDataFrame
        """
        properties = {
            TRAJ_ID_COL_NAME: self.id,
            "start_t": self.get_start_time(),
            "end_t": self.get_end_time(),
            "geometry": self.to_linestring(),
            "length": self.get_length(),
            "direction": self.get_direction(),
        }

        if wkt:
            properties["wkt"] = self.to_linestringm_wkt()
        if agg:
            for col, agg_modes in agg.items():
                if type(agg_modes) != list:
                    agg_modes = [agg_modes]
                for agg_mode in agg_modes:
                    if agg_mode == "mode":
                        aggregated = self.df.agg({col: Series.mode})[col][0]
                    elif agg_mode[0] == "q" and int(agg_mode[1:]) < 100:
                        aggregated = self.df[col].agg(
                            lambda x: x.quantile(int(agg_mode[1:]) / 100)
                        )
                    else:
                        aggregated = self.df.agg({col: agg_mode})[col]
                    properties[f"{col}_{agg_mode}"] = aggregated
        df = DataFrame([properties])
        traj_gdf = GeoDataFrame(df, crs=self.crs)
        return traj_gdf

    def get_start_location(self):
        """
        Return the trajectory's start location.

        Returns
        -------
        shapely Point
            Trajectory start location
        """
        return self.df.geometry.iloc[0]

    def get_end_location(self):
        """Return the trajectory's end location.

        Returns
        -------
        shapely Point
            Trajectory end location
        """
        return self.df.geometry.iloc[-1]

    def get_bbox(self):
        """
        Return the trajectory's bounding box.

        Returns
        -------
        tuple
            Bounding box values (minx, miny, maxx, maxy)
        """
        return tuple(self.df.total_bounds.tolist())  # (minx, miny, maxx, maxy)

    def get_start_time(self):
        """
        Return the trajectory's start time.

        Returns
        -------
        datetime.datetime
            Trajectory start time
        """
        return self.df.index.min().to_pydatetime()

    def get_end_time(self):
        """
        Return the trajectory's end time.

        Returns
        -------
        datetime.datetime
            Trajectory end time
        """
        return self.df.index.max().to_pydatetime()

    def get_duration(self):
        """
        Return the trajectory's duration from start to end.

        Returns
        -------
        datetime.timedelta
            Trajectory duration
        """
        return self.get_end_time() - self.get_start_time()

    def get_row_at(self, t, method="nearest"):
        """
        Return row of the trajectory's DataFrame at time t.

        Parameters
        ----------
        t : datetime.datetime
            Timestamp to extract a row for
        method : str
            Interpolation method (Pandas get_loc method)

        Returns
        -------
        Pandas series
            Row of the DataFrame at time t
        """
        try:
            return self.df.loc[t]
        except KeyError:
            index = self.df.index.sort_values().drop_duplicates()
            idx = index.get_indexer([t], method=method)[0]
            return self.df.iloc[idx]

    def interpolate_position_at(self, t):
        """
        Compute and return interpolated position at time t.

        Parameters
        ----------
        t : datetime.datetime
            Timestamp to interpolate at

        Returns
        -------
        shapely Point
            Interpolated position along the trajectory at time t
        """
        prev_row = self.get_row_at(t, "ffill")
        next_row = self.get_row_at(t, "bfill")
        t_diff = next_row.name - prev_row.name
        t_diff_at = t - prev_row.name
        line = LineString(
            [
                prev_row[self.get_geom_column_name()],
                next_row[self.get_geom_column_name()],
            ]
        )
        if t_diff == 0 or line.length == 0:
            return prev_row[self.get_geom_column_name()]
        interpolated_position = line.interpolate(t_diff_at / t_diff * line.length)
        return interpolated_position

    def get_position_at(self, t, method="interpolated"):
        """
        Compute and return position at time t.

        Parameters
        ----------
        t : datetime.datetime
            Timestamp to extract a row for
        method : str
            Interpolation method

        Returns
        -------
        shapely Point
            Position at time t

        Examples
        --------
        If the trajectory contains a position at the given timestamp, it is
        returned:

        >>> traj.get_position_at(datetime(2018, 1, 1, 12, 6))
        Point (6 0)

        If there is no trajectory position for the given timestamp, the default
        behaviour is to interpolate the location:

        >>> traj.get_position_at(datetime(2018, 1, 1, 12, 9))
        POINT (6 4.5)

        To get the trajectory position closest to the given timestamp, specify
        method='nearest':

        >>> traj.get_position_at(datetime(2018, 1, 1, 12, 9), method='nearest')
        POINT (6 6)
        """
        if t < self.get_start_time() or t > self.get_end_time():
            raise ValueError(
                f"Timestamp {t} outside the trajectory's time range "
                f"({self.get_start_time()} to {self.get_end_time()})"
            )
        if method not in ["nearest", "interpolated", "ffill", "bfill"]:
            raise ValueError(
                f"Invalid method {method}. Must be one of [nearest, interpolated, "
                "ffill, bfill]"
            )
        if method == "interpolated":
            return self.interpolate_position_at(t)
        else:
            row = self.get_row_at(t, method)
            try:
                return row[self.get_geom_column_name()][0]
            except TypeError:
                return row[self.get_geom_column_name()]

    def get_linestring_between(self, t1, t2, method="interpolated"):
        """
        Return LineString of segment between times t1 and t2.

        Parameters
        ----------
        t1 : datetime.datetime
            Start time for the segment
        t2 : datetime.datetime
            End time for the segment
        method : str
            Extraction method

        Returns
        -------
        shapely LineString
            Extracted trajectory segment
        """
        if method not in ["interpolated", "within"]:
            raise ValueError(
                "Invalid split method {}. Must be one of [interpolated, within]".format(
                    method
                )
            )
        if method == "interpolated":
            st_range = SpatioTemporalRange(
                self.get_position_at(t1), self.get_position_at(t2), t1, t2
            )
            temp_df = create_entry_and_exit_points(self, st_range)
            temp_df = temp_df[t1:t2]
            return point_gdf_to_linestring(temp_df, self.get_geom_column_name())
        else:
            try:
                return point_gdf_to_linestring(
                    self.get_segment_between(t1, t2).df, self.get_geom_column_name()
                )
            except RuntimeError:
                raise RuntimeError(
                    "Cannot generate linestring between {0} and {1}".format(t1, t2)
                )

    def get_segment_between(self, t1, t2):
        """
        Return Trajectory segment between times t1 and t2.

        Parameters
        ----------
        t1 : datetime.datetime
            Start time for the segment
        t2 : datetime.datetime
            End time for the segment

        Returns
        -------
        Trajectory
            Extracted trajectory segment
        """
        segment = Trajectory(self.df[t1:t2], "{}_{}".format(self.id, t1), parent=self)
        if not segment.is_valid():
            raise RuntimeError(
                "Failed to extract valid trajectory segment between {} and {}".format(
                    t1, t2
                )
            )
        return segment

    def _compute_distance(self, row, conversion):
        pt0 = row["prev_pt"]
        pt1 = row[self.get_geom_column_name()]
        if not isinstance(pt0, Point):
            return 0.0
        if not isinstance(pt1, Point):
            raise ValueError("Invalid trajectory! Got {} instead of point!".format(pt1))
        if pt0 == pt1:
            return 0.0
        if self.is_latlon:
            dist_computed = (
                measure_distance_geodesic(pt0, pt1)
                * conversion.crs
                / conversion.distance
            )
        else:  # The following distance will be in CRS units that might not be meters!
            dist_computed = (
                measure_distance_euclidean(pt0, pt1)
                * conversion.crs
                / conversion.distance
            )
        return dist_computed

    def _add_prev_pt(self, force=True):
        """
        Create a shifted geometry column with previous positions.
        """
        if "prev_pt" not in self.df.columns or force:
            # TODO: decide on default enforcement behavior
            self.df = self.df.assign(prev_pt=self.df.geometry.shift())

    def get_length(self):
        """
        Return the length of the trajectory.

        Length is calculated using CRS units, except if the CRS is geographic
        (e.g. EPSG:4326 WGS84) then length is calculated in metres.

        Returns
        -------
        float
            Length of the trajectory
        """
        pt_tuples = [(pt.y, pt.x) for pt in self.df.geometry.tolist()]
        if self.is_latlon:
            return geodesic(*pt_tuples).m
        else:  # The following distance will be in CRS units that might not be meters!
            return LineString(pt_tuples).length

    def get_direction(self):
        """
        Return the direction of the trajectory.

        The direction is calculated between the trajectory's start and end
        location. Direction values are in degrees, starting North turning
        clockwise.

        Returns
        -------
        float
            Direction of the trajectory in degrees
        """
        pt0 = self.get_start_location()
        pt1 = self.get_end_location()
        if self.is_latlon:
            return calculate_initial_compass_bearing(pt0, pt1)
        else:
            return azimuth(pt0, pt1)

    def get_sampling_interval(self):
        """
        Return the sampling interval of the trajectory.

        The sampling interval is computed as the median time difference between
        consecutive rows in the trajectory's DataFrame.

        Returns
        -------
        datetime.timedelta
            Sampling interval
        """
        if hasattr(self, "timedelta_col_name"):
            if self.timedelta_col_name in self.df.columns:
                return self.df[self.timedelta_col_name].median()
        return self._get_df_with_timedelta()[TIMEDELTA_COL_NAME].median()

    def _compute_heading(self, row):
        pt0 = row["prev_pt"]
        pt1 = row[self.get_geom_column_name()]
        if not isinstance(pt0, Point):
            return 0.0
        if pt0 == pt1:
            return 0.0
        if self.is_latlon:
            return calculate_initial_compass_bearing(pt0, pt1)
        else:
            return azimuth(pt0, pt1)

    def _compute_angular_difference(self, row):
        degrees1 = row["prev_direction"]
        degrees2 = row["direction"]
        if degrees1 == degrees2:
            return 0.0
        else:
            return angular_difference(degrees1, degrees2)

    def _compute_speed(self, row, conversion):
        pt0 = row["prev_pt"]
        pt1 = row[self.get_geom_column_name()]
        if not isinstance(pt0, Point):
            return 0.0
        if not isinstance(pt1, Point):
            raise ValueError("Invalid trajectory! Got {} instead of point!".format(pt1))
        if pt0 == pt1:
            return 0.0
        if self.is_latlon:
            dist_computed = (
                measure_distance_geodesic(pt0, pt1)
                * conversion.crs
                / conversion.distance
            )
        else:  # The following distance will be in CRS units that might not be meters!
            dist_computed = (
                measure_distance_euclidean(pt0, pt1)
                * conversion.crs
                / conversion.distance
            )
        return dist_computed / row["delta_t"].total_seconds() * conversion.time

    def _connect_prev_pt_and_geometry(self, row):
        pt0 = row["prev_pt"]
        pt1 = row[self.get_geom_column_name()]
        if not isinstance(pt0, Point):
            return None
        if not isinstance(pt1, Point):
            raise ValueError("Invalid trajectory! Got {} instead of point!".format(pt1))
        if pt0 == pt1:
            # to avoid intersection issues with zero length lines
            pt1 = translate(pt1, 0.00000001, 0.00000001)
        return LineString(list(pt0.coords) + list(pt1.coords))

    def add_traj_id(self, overwrite=False):
        """
        Add trajectory id column and values to the trajectory's DataFrame.

        Parameters
        ----------
        overwrite : bool
            Whether to overwrite existing trajectory id values (default: False)
        """
        if TRAJ_ID_COL_NAME in self.df.columns and not overwrite:
            raise RuntimeError(
                f"Trajectory already contains a {TRAJ_ID_COL_NAME} column! "
                f"Use overwrite=True to overwrite exiting values."
            )
        self.df[TRAJ_ID_COL_NAME] = self.id

    def add_direction(self, overwrite=False, name=DIRECTION_COL_NAME):
        """
        Add direction column and values to the trajectory's DataFrame.

        The direction is calculated between consecutive locations.
        Direction values are in degrees, starting North turning clockwise.
        Values are [0, 360).

        Parameters
        ----------
        overwrite : bool
            Whether to overwrite existing direction values (default: False)
        """
        self.direction_col_name = name
        if self.direction_col_name in self.df.columns and not overwrite:
            raise RuntimeError(
                f"Trajectory already has a column named {self.direction_col_name}! "
                "Use overwrite=True to overwrite exiting values or update the "
                "name arg."
            )
        self._add_prev_pt()
        self.df[name] = self.df.apply(self._compute_heading, axis=1)
        # set the direction in the first row to the direction of the second row
        self.df.at[self.get_start_time(), name] = self.df.iloc[1][name]
        self.df.drop(columns=["prev_pt"], inplace=True)

    def add_angular_difference(
        self,
        overwrite=False,
        name=ANGULAR_DIFFERENCE_COL_NAME,
    ):
        """
        Add angular difference to the trajectory's DataFrame.

        Angular difference is calculated as the absolute smaller angle
        between direction for points along the trajectory.
        Values are [0, 180.0]
        """
        self.angular_difference_col_name = name
        if self.angular_difference_col_name in self.df.columns and not overwrite:
            raise RuntimeError(
                f"Trajectory already has a column named "
                f"{self.angular_difference_col_name}!"
                f"Use overwrite=True to overwrite exiting values or update the "
                f"name arg."
            )
        # Avoid computing direction again if already computed
        direction_column_name = self.get_direction_column_name()
        if direction_column_name in self.df.columns:
            direction_exists = True
            temp_df = self.df.copy()
        else:
            direction_exists = False
            self.add_direction(name=DIRECTION_COL_NAME)
            temp_df = self.df.copy()

        temp_df["prev_" + direction_column_name] = temp_df[
            direction_column_name
        ].shift()
        self.df[name] = temp_df.apply(self._compute_angular_difference, axis=1)
        # set the first row to be 0
        self.df.at[self.get_start_time(), name] = 0.0
        if not direction_exists:
            self.df.drop(columns=[DIRECTION_COL_NAME], inplace=True)

    def add_distance(self, overwrite=False, name=DISTANCE_COL_NAME, units=None):
        """
        Add distance column and values to the trajectory's DataFrame.

        Adds a column with the distance to each point from the previous:
            If no units have been declared:
                For geographic projections (e.g. EPSG:4326 WGS84), in meters
                For other projections, in CRS units
            If units have been declared:
                For geographic projections, in declared units
                For known CRS units, in declared units
                For unknown CRS units, in declared units as if CRS is in meters

        Parameters
        ----------
        overwrite : bool
            Whether to overwrite existing distance values (default: False)

        units : str
            Units in which to calculate distance values (default: CRS units)
            Allowed:
                "km": Kilometer
                "m": metre
                "dm": Decimeter
                "cm": Centimeter
                "mm": Millimeter
                "nm": International Nautical Mile
                "inch": International Inch
                "ft": International Foot
                "yd": International Yard
                "mi": International Statute Mile
                "link": International Link
                "chain": International Chain
                "fathom": International Fathom
                "british_ft": British foot (Sears 1922)
                "british_yd": British yard (Sears 1922)
                "british_chain_sears": British chain (Sears 1922)
                "british_link_sears": British link (Sears 1922)
                "sears_yd": Yard (Sears)
                "link_sears": Link (Sears)
                "chain_sears": Chain (Sears)
                "british_ft_sears_truncated": British foot (Sears 1922 truncated)
                "british_chain_sears_truncated": British chain (Sears 1922 truncated)
                "british_chain_benoit": British chain (Benoit 1895 B)
                "chain_benoit": Chain (Benoit)
                "link_benoit": Link (Benoit)
                "clarke_yd": Clarke's yard
                "clarke_ft": Clarke’s Foot
                "clarke_link": Clarke’s link
                "clarke_chain": Clarke's chain
                "british_ft_1936": British foot (1936)
                "gold_coast_ft": Gold Coast foot
                "rod": Rod
                "furlong": Furlong
                "german_m": German legal metre
                "survey_in": US survey inch
                "survey_ft": US survey foot
                "survey_yd": US survey yard
                "survey_lk": US survey link
                "survey_ch": US survey chain
                "survey_mi": US survey mile
                "indian_yd": Indian Yard
                "indian_ft": Indian Foot
                "indian_ft_1937": Indian Foot 1937
                "indian_ft_1962": Indian Foot 1962
                "indian_ft_1975": Indian Foot 1975

        Examples
        ----------
        If no units are declared, the distance will be calculated
        in meters for geographic projections (e.g. EPSG:4326 WGS84)
        and in CRS units for all other projections

        >>>traj.add_distance()

        The default column name is "distance". If a column of this name
        already exists, a new column name can be specified

        >>>traj.add_distance(name="distance (CRS units)")

        If units are declared, the distance will be calculated in those units
        except if the CRS units are unknown, in which case the CRS units are
        assumed to be in meters

        >>>traj.add_distance(units="km")

        It is suggested to declare a name for the new column specifying units

        >>>traj.add_distance(name="distance (miles)", units="mi")
        >>>traj.add_distance(name="distance (US Survey Feet)", units="survey_ft")
        """
        self.distance_col_name = name
        if self.distance_col_name in self.df.columns and not overwrite:
            raise RuntimeError(
                f"Trajectory already has a column named {self.distance_col_name}! "
                "Use overwrite=True to overwrite exiting values or update the "
                "name arg."
            )
        if isinstance(units, tuple):
            units = UNITS(*units)
        else:
            units = UNITS(units)
        conversion = get_conversion(units, self.crs_units)
        self.df = self._get_df_with_distance(conversion, name)

    def add_speed(self, overwrite=False, name=SPEED_COL_NAME, units=UNITS()):
        """
        Add speed column and values to the trajectory's DataFrame.

        Adds a column with the speed to each point from the previous:
             If no units have been declared:
                 For geographic projections, in meters per second
                 For other projections, in CRS units per second
             If units have been declared:
                 For geographic projections, in declared units
                 For known CRS units, in declared units
                 For unknown CRS units, in declared units as if CRS distance
                 units are meters

        Parameters
        ----------
        overwrite : bool
            Whether to overwrite existing speed values (default: False)
        name : str
            Name of the speed column (default: "speed")
        units : tuple
            Units in which to calculate speed
            distance : str
                Abbreviation for the distance unit
                (default: CRS units, or metres if geographic)
            time : str
                Abbreviation for the time unit (default: seconds)

            Allowed distance units:
                "km": Kilometer
                "m": metre
                "dm": Decimeter
                "cm": Centimeter
                "mm": Millimeter
                "nm": International Nautical Mile
                "inch": International Inch
                "ft": International Foot
                "yd": International Yard
                "mi": International Statute Mile
                "link": International Link
                "chain": International Chain
                "fathom": International Fathom
                "british_ft": British foot (Sears 1922)
                "british_yd": British yard (Sears 1922)
                "british_chain_sears": British chain (Sears 1922)
                "british_link_sears": British link (Sears 1922)
                "sears_yd": Yard (Sears)
                "link_sears": Link (Sears)
                "chain_sears": Chain (Sears)
                "british_ft_sears_truncated": British foot (Sears 1922 truncated)
                "british_chain_sears_truncated": British chain (Sears 1922 truncated)
                "british_chain_benoit": British chain (Benoit 1895 B)
                "chain_benoit": Chain (Benoit)
                "link_benoit": Link (Benoit)
                "clarke_yd": Clarke's yard
                "clarke_ft": Clarke’s Foot
                "clarke_link": Clarke’s link
                "clarke_chain": Clarke's chain
                "british_ft_1936": British foot (1936)
                "gold_coast_ft": Gold Coast foot
                "rod": Rod
                "furlong": Furlong
                "german_m": German legal metre
                "survey_in": US survey inch
                "survey_ft": US survey foot
                "survey_yd": US survey yard
                "survey_lk": US survey link
                "survey_ch": US survey chain
                "survey_mi": US survey mile
                "indian_yd": Indian Yard
                "indian_ft": Indian Foot
                "indian_ft_1937": Indian Foot 1937
                "indian_ft_1962": Indian Foot 1962
                "indian_ft_1975": Indian Foot 1975

            Allowed time units:
                "s": seconds
                "min": minutes
                "h": hours
                "d": days
                "a": years

        Examples
        ----------
        If no units are declared, the speed will be calculated
        in meters per second for geographic projections (e.g. EPSG:4326 WGS84)
        and in CRS distance units per second for all other projections

        >>>traj.add_speed()

        The default column name is "speed". If a column of this name
        already exists, a new column name can be specified

        >>>traj.add_speed(name="speed (CRS units)")

        If units are declared, the speed will be calculated in those units
        except if the CRS distance units are unknown, in which case the
        CRS distance units are assumed to be meters

        >>>traj.add_speed(units=("km", "h"))

        It is suggested to declare a name for the new column specifying units

        >>>traj.add_speed(name="speed (mph)", units=("mi", "h"))
        >>>traj.add_speed(name="US Survey Feet/s", units=("survey_ft", "s"))
        """
        self.speed_col_name = name
        if self.speed_col_name in self.df.columns and not overwrite:
            raise RuntimeError(
                f"Trajectory already has a column named {self.speed_col_name}! "
                f"Use overwrite=True to overwrite exiting values or update the "
                f"name arg."
            )
        if isinstance(units, tuple):
            units = UNITS(*units)
        else:
            units = UNITS(units)
        conversion = get_conversion(units, self.crs_units)
        self.df = self._get_df_with_speed(conversion, name)

    def add_acceleration(
        self, overwrite=False, name=ACCELERATION_COL_NAME, units=UNITS()
    ):
        """
        Add acceleration column and values to the trajectory's DataFrame.

        Adds a column with the acceleration to each point from the previous:
             If no units have been declared:
                 For geographic projections, in meters per second squared
                 For other projections, in CRS units per second squared
             If units have been declared:
                 For geographic projections, using declared units
                 For known CRS units, using declared units
                 For unknown CRS units, using declared units as if CRS distance
                 units are meters
                 If only distance units are declared, returns
                     distance per second squared
                 If distance and one time unit declared, returns
                     distance/time per second

        Parameters
        ----------
        overwrite : bool
            Whether to overwrite existing speed values (default: False)
        name : str
            Name of the acceleration column (default: "acceleration")
        units : tuple
            Units in which to calculate acceleration
            distance : str
                Abbreviation for the distance unit
                (default: CRS units, or metres if geographic)
            time : str
                Abbreviation for the time unit (default: seconds)
            time2 : str
                Abbreviation for the second time unit (default: seconds)

            Allowed distance units:
                "km": Kilometer
                "m": metre
                "dm": Decimeter
                "cm": Centimeter
                "mm": Millimeter
                "nm": International Nautical Mile
                "inch": International Inch
                "ft": International Foot
                "yd": International Yard
                "mi": International Statute Mile
                "link": International Link
                "chain": International Chain
                "fathom": International Fathom
                "british_ft": British foot (Sears 1922)
                "british_yd": British yard (Sears 1922)
                "british_chain_sears": British chain (Sears 1922)
                "british_link_sears": British link (Sears 1922)
                "sears_yd": Yard (Sears)
                "link_sears": Link (Sears)
                "chain_sears": Chain (Sears)
                "british_ft_sears_truncated": British foot (Sears 1922 truncated)
                "british_chain_sears_truncated": British chain (Sears 1922 truncated)
                "british_chain_benoit": British chain (Benoit 1895 B)
                "chain_benoit": Chain (Benoit)
                "link_benoit": Link (Benoit)
                "clarke_yd": Clarke's yard
                "clarke_ft": Clarke’s Foot
                "clarke_link": Clarke’s link
                "clarke_chain": Clarke's chain
                "british_ft_1936": British foot (1936)
                "gold_coast_ft": Gold Coast foot
                "rod": Rod
                "furlong": Furlong
                "german_m": German legal metre
                "survey_in": US survey inch
                "survey_ft": US survey foot
                "survey_yd": US survey yard
                "survey_lk": US survey link
                "survey_ch": US survey chain
                "survey_mi": US survey mile
                "indian_yd": Indian Yard
                "indian_ft": Indian Foot
                "indian_ft_1937": Indian Foot 1937
                "indian_ft_1962": Indian Foot 1962
                "indian_ft_1975": Indian Foot 1975

            Allowed time units:
                "s": seconds
                "min": minutes
                "h": hours
                "d": days
                "a": years

        Examples
        ----------
        If no units are declared, the acceleration will be calculated
        in meters per second squared for geographic projections and
        in CRS distance units per second squared for all other projections

        >>>traj.add_acceleration()

        The default column name is "acceleration". If a column of this name
        already exists, a new column name can be specified

        >>>traj.add_acceleration(name="acceleration (CRS units/s2)")

        If units are declared, acceleration will be calculated in those units
        except if the CRS distance units are unknown, in which case the
        CRS distance units are assumed to be meters

        >>>traj.add_acceleration(units=("km", "h", "s"))

        It is suggested to declare a name for the new column specifying units

        >>>traj.add_acceleration(name="US Survey Feet/s2", units=("survey_ft", "s"))
        >>>traj.add_acceleration(name="mph/s", units=("mi", "h", "s"))
        """
        self.acceleration_col_name = name
        if self.acceleration_col_name in self.df.columns and not overwrite:
            raise RuntimeError(
                f"Trajectory already has a column named {self.acceleration_col_name}! "
                f"Use overwrite=True to overwrite exiting values or update the "
                f"name arg."
            )
        if isinstance(units, tuple):
            units = UNITS(*units)
        else:
            units = UNITS(units)
        conversion = get_conversion(units, self.crs_units)
        self.df = self._get_df_with_acceleration(conversion, name)

    def add_timedelta(self, overwrite=False, name=TIMEDELTA_COL_NAME):
        """
        Add timedelta column and values to the trajectory's DataFrame.

        Timedelta is calculated as the time difference between the current
        and the previous row. Values are instances of datetime.timedelta.

        Parameters
        ----------
        overwrite : bool
            Whether to overwrite existing timedelta values (default: False)
        name : str
            Name of the timedelta column (default: "timedelta")
        """
        self.timedelta_col_name = name
        if self.timedelta_col_name in self.df.columns and not overwrite:
            raise RuntimeError(
                f"Trajectory already has a column named {self.timedelta_col_name}! "
                f"Use overwrite=True to overwrite exiting values or update the "
                f"name arg."
            )
        self.df = self._get_df_with_timedelta(name)

    def _get_df_with_timedelta(self, name=TIMEDELTA_COL_NAME):
        temp_df = self.df.copy()
        times = Series(index=temp_df.index, data=temp_df.index)
        temp_df[name] = times.diff().values
        return temp_df

    def _get_df_with_distance(self, conversion, name=DISTANCE_COL_NAME):
        temp_df = self.df.copy()
        temp_df = temp_df.assign(prev_pt=temp_df.geometry.shift())
        try:
            temp_df[name] = temp_df.apply(
                self._compute_distance, conversion=conversion, axis=1
            )
        except ValueError as e:
            raise e
        # set the distance in the first row to zero
        temp_df.at[self.get_start_time(), name] = 0
        temp_df = temp_df.drop(columns=["prev_pt"])
        return temp_df

    def _get_df_with_speed(self, conversion, name=SPEED_COL_NAME):
        temp_df = self._get_df_with_timedelta(name="delta_t")
        temp_df = temp_df.assign(prev_pt=temp_df.geometry.shift())
        try:
            temp_df[name] = temp_df.apply(
                self._compute_speed, conversion=conversion, axis=1
            )
        except ValueError as e:
            raise e
        # set the speed in the first row to the speed of the second row
        temp_df.at[self.get_start_time(), name] = temp_df.iloc[1][name]
        temp_df = temp_df.drop(columns=["prev_pt", "delta_t"])
        return temp_df

    def _get_df_with_acceleration(self, conversion, name=ACCELERATION_COL_NAME):
        temp_df = self._get_df_with_speed(conversion, name="speed_temp")
        temp_df[name] = (
            temp_df["speed_temp"].diff()
            / temp_df.index.to_series().diff().dt.total_seconds()
            * conversion.time2
        )
        # set the acceleration in the first row to the acceleration of the
        # second row
        temp_df.at[self.get_start_time(), name] = temp_df.iloc[1][name]
        return temp_df.drop(columns=["speed_temp"])

    def intersects(self, polygon):
        """
        Return whether the trajectory intersects the given polygon.

        Parameters
        ----------
        polygon : shapely.geometry.Polygon
            Polygon to test for intersections

        Returns
        -------
        bool
        """
        return intersects(self, polygon)

    def distance(self, other):
        """
        Return the minimum distance to the other geometric object (based on shapely
        https://shapely.readthedocs.io/en/stable/manual.html#object.distance).

        Parameters
        ----------
        other : shapely.geometry or Trajectory
            Other geometric object or trajectory

        Returns
        -------
        float
            Distance
        """
        if self.is_latlon:
            message = (
                f"Distance is computed using Euclidean geometry but "
                f"the trajectory coordinate system is {self.crs}."
            )
            warnings.warn(message, UserWarning)
        if type(other) == Trajectory:
            other = other.to_linestring()
        return self.to_linestring().distance(other)

    def hausdorff_distance(self, other):
        """
        Return the Hausdorff distance to the other geometric object (based on shapely
        https://shapely.readthedocs.io/en/stable/manual.html#object.hausdorff_distance).
        The Hausdorff distance between two geometries is the furthest distance
        that a point on either geometry can be from the nearest point to it on
        the other geometry.

        Parameters
        ----------
        other : shapely.geometry or Trajectory
            Other geometric object or trajectory

        Returns
        -------
        float
            Hausdorff distance
        """
        if self.is_latlon:
            message = (
                f"Hausdorff distance is computed using Euclidean geometry but "
                f"the trajectory coordinate system is {self.crs}."
            )
            warnings.warn(message, UserWarning)
        if type(other) == Trajectory:
            other = other.to_linestring()
        return self.to_linestring().hausdorff_distance(other)

    def clip(self, polygon, point_based=False):
        """
        Return trajectory segments clipped by the given polygon.

        By default, the trajectory's line representation is clipped by the
        polygon. If pointbased=True, the trajectory's point representation is
        used instead, leading to shorter segments.

        Parameters
        ----------
        polygon : shapely Polygon
            Polygon to clip with
        point_based : bool
            Clipping method

        Returns
        -------
        TrajectoryCollection
            Clipped trajectory segments
        """
        from .trajectory_collection import TrajectoryCollection

        segments = clip(self, polygon, point_based)
        return TrajectoryCollection(segments)

    def intersection(self, feature, point_based=False):
        """
        Return the trajectory segments that intersects the given feature.

        Feature attributes are appended to the trajectory's DataFrame.

        By default, the trajectory's line representation is clipped by the
        polygon. If pointbased=True, the trajectory's point representation is
        used instead, leading to shorter segments.

        Parameters
        ----------
        feature : shapely Feature
            Feature to intersect with
        point_based : bool
            Clipping method

        Returns
        -------
        TrajectoryCollection
            Segments intersecting with the feature
        """
        from .trajectory_collection import TrajectoryCollection

        segments = intersection(self, feature, point_based)
        return TrajectoryCollection(segments)

    def apply_offset_seconds(self, column, offset):
        """
        Shift column by the specified offset in seconds.

        Parameters
        ----------
        column : str
            Name of the column to shift
        offset : int
            Number of seconds to shift by, can be positive or negative
        """
        self.df[column] = self.df[column].shift(offset, freq="1s")

    def apply_offset_minutes(self, column, offset):
        """
        Shift column by the specified offset in minutes.

        Parameters
        ----------
        column : str
            Name of the column to shift
        offset : int
            Number of minutes to shift by, can be positive or negative
        """
        self.df[column] = self.df[column].shift(offset, freq="1min")

    def _to_line_df(self):
        """
        Convert trajectory data GeoDataFrame of points to GeoDataFrame of lines
        that connect consecutive points.

        Returns
        -------
        line_df : GeoDataFrame
            GeoDataFrame of line segments
        """
        line_df = self.df.copy()
        line_df["prev_pt"] = line_df.geometry.shift()
        line_df["t"] = self.df.index
        line_df["prev_t"] = line_df["t"].shift()
        line_df["line"] = line_df.apply(self._connect_prev_pt_and_geometry, axis=1)
        line_df = line_df.set_geometry("line")[1:]
        return line_df

    def get_mcp(self):
        """Return the Minimum Convex Polygon of the trajectory data

        Returns
        -------
        mcp : Shapely object
            The polygon or line (in case of only two points)
            of the Minimum Convex Polygon
        """
        return self.df.geometry.unary_union.convex_hull
