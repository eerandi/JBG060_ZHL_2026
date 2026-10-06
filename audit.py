"""
Data audit script for cleaning and validating raw datasets.
"""

import os
import io
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import xarray as xr

warnings.filterwarnings("ignore")

ISSUES: list[str] = []


def flag(source: str, msg: str) -> None:
    entry = f"[{source}] {msg}"
    ISSUES.append(entry)
    print("  ISSUE:", msg)


# Auditors — each prints findings and calls flag() for any issues found

def audit_dartmouth() -> None:
    print("\n=== 1. Dartmouth Flood Observatory ===")
    base = Path("./raw_data/Darthmouth Flood Observatory")

    if not base.exists():
        flag("Dartmouth", "Directory missing — raw data not downloaded")
        return

    info_path = base / "information.xlsx"
    if not info_path.exists():
        flag("Dartmouth", "information.xlsx missing")
        return

    info = pd.read_excel(info_path)
    required_cols = {"area id", "station number", "country", "latitude", "longitude"}
    missing_cols = required_cols - set(info.columns.str.lower())
    if missing_cols:
        flag("Dartmouth", f"information.xlsx missing columns: {missing_cols}")

    area_ids = info["area id"].values
    csv_files = list(base.glob("*_discharge.csv"))
    csv_ids = {int(f.stem.split("_")[0]) for f in csv_files}

    missing_csvs = set(area_ids) - csv_ids
    extra_csvs = csv_ids - set(area_ids)
    if missing_csvs:
        flag("Dartmouth", f"{len(missing_csvs)} station(s) in information.xlsx have no CSV: {missing_csvs}")
    if extra_csvs:
        flag("Dartmouth", f"{len(extra_csvs)} CSV(s) not listed in information.xlsx: {extra_csvs}")

    date_ranges = {}
    for area_id in area_ids:
        fpath = base / f"{area_id}_discharge.csv"
        if not fpath.exists():
            continue

        df = pd.read_csv(fpath)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        src = f"Dartmouth/{area_id}"

        bad_dates = df["Date"].isna().sum()
        if bad_dates:
            flag(src, f"{bad_dates} rows with unparseable dates")

        df = df.dropna(subset=["Date"]).set_index("Date").sort_index()
        col = df.columns[0]

        dupes = df.index.duplicated().sum()
        if dupes:
            flag(src, f"{dupes} duplicate timestamps")

        neg = (df[col] < 0).sum()
        if neg:
            flag(src, f"{neg} rows with negative discharge")

        nans = df[col].isna().sum()
        if nans:
            flag(src, f"{nans} NaN discharge values ({100*nans/len(df):.1f}%)")

        idx = df.index.drop_duplicates().sort_values()
        gaps = pd.Series(idx).diff().dt.days
        big_gaps = gaps[gaps > 3]
        if len(big_gaps):
            flag(src, f"{len(big_gaps)} gap(s) > 3 days (max {int(big_gaps.max())} days)")

        date_ranges[area_id] = (df.index.min(), df.index.max())

    if date_ranges:
        starts = pd.Series({k: v[0] for k, v in date_ranges.items()})
        ends   = pd.Series({k: v[1] for k, v in date_ranges.items()})
        span_years = (ends - starts).dt.days / 365.25
        short = span_years[span_years < 5]
        if len(short):
            flag("Dartmouth", f"{len(short)} station(s) have < 5 years of data: {short.index.tolist()}")
        print(f"  Stations: {len(area_ids)} listed, {len(csv_files)} CSVs found")
        print(f"  Date range across stations: {starts.min().date()} – {ends.max().date()}")

    if "latitude" in info.columns and "longitude" in info.columns:
        out_lat = info[(info["latitude"] < -90) | (info["latitude"] > 90)]
        out_lon = info[(info["longitude"] < -180) | (info["longitude"] > 180)]
        if len(out_lat):
            flag("Dartmouth/info", f"{len(out_lat)} row(s) with invalid latitude")
        if len(out_lon):
            flag("Dartmouth/info", f"{len(out_lon)} row(s) with invalid longitude")

    if not [i for i in ISSUES if "Dartmouth" in i]:
        print("  No issues found.")


def audit_lakes() -> None:
    print("\n=== 2. Lake water levels ===")
    base = Path("./raw_data/Water levels lakes")

    if not base.exists():
        flag("Lakes", "Directory missing — raw data not downloaded")
        return

    albert_path = base / "water_level_altimetry_Albert.nc"
    if not albert_path.exists():
        flag("Lakes/Albert", "water_level_altimetry_Albert.nc missing")
    else:
        ds = xr.open_dataset(albert_path)
        df = ds.to_dataframe().reset_index()
        if "datetime" not in df.columns and "time" not in df.columns:
            flag("Lakes/Albert", "no datetime/time column found in NetCDF")
        else:
            time_col = "datetime" if "datetime" in df.columns else "time"
            df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
            bad = df[time_col].isna().sum()
            if bad:
                flag("Lakes/Albert", f"{bad} unparseable timestamps")
            numeric_cols = df.select_dtypes(include=np.number).columns.tolist()
            for c in numeric_cols:
                nans = df[c].isna().sum()
                if nans:
                    flag("Lakes/Albert", f"Column '{c}': {nans} NaNs ({100*nans/len(df):.1f}%)")
            print(f"  Albert: {len(df)} records, {df[time_col].min()} – {df[time_col].max()}")
        ds.close()

    mission_names = {"TOPEX", "JASON-1", "JASON-2", "JASON-3", "J1", "J2",
                     "J3", "S3A", "S3B", "S6A", "ENVISAT", "ERS-1", "ERS-2"}
    col_names = ["mission", "cycle", "date", "hour", "minute",
                 "height_wrt_ref", "height_err", "backscatter_ku",
                 "wet_tropo_corr", "iono_corr", "dry_tropo_corr",
                 "mode1", "mode2", "ice_flag", "height_egm2008", "data_source_flag"]

    for lake in ["victoria", "Kyoga"]:
        fname = base / f"water_level_{lake}.txt"
        if not fname.exists():
            flag(f"Lakes/{lake}", f"water_level_{lake}.txt missing")
            continue

        data_lines = []
        with open(fname, "r", encoding="latin-1") as f:
            for line in f:
                first_token = line.strip().split()[0] if line.strip() else ""
                if first_token in mission_names:
                    data_lines.append(line.strip())

        if not data_lines:
            flag(f"Lakes/{lake}", "No mission data lines found in file")
            continue

        df = pd.read_csv(
            io.StringIO("\n".join(data_lines)),
            sep=r"\s+",
            names=col_names,
            na_values=["999.99", "99.999", "9999.99"],
        )
        df_clean = df.dropna()
        dropped = len(df) - len(df_clean)
        if dropped:
            flag(f"Lakes/{lake}", f"{dropped} rows dropped due to sentinel NaN values ({100*dropped/len(df):.1f}%)")

        df_clean = df_clean.copy()
        df_clean["date"] = pd.to_datetime(df_clean["date"].astype(str), format="%Y%m%d", errors="coerce")
        bad_dates = df_clean["date"].isna().sum()
        if bad_dates:
            flag(f"Lakes/{lake}", f"{bad_dates} unparseable dates")

        # Height anomalies — extreme outliers beyond ±50 m from median
        h = df_clean["height_wrt_ref"].dropna()
        med = h.median()
        outliers = ((h - med).abs() > 50).sum()
        if outliers:
            flag(f"Lakes/{lake}", f"{outliers} height values > 50 m from median (possible outliers)")

        dates = df_clean["date"].dropna().sort_values()
        gaps = dates.diff().dt.days
        big_gaps = gaps[gaps > 90]
        if len(big_gaps):
            flag(f"Lakes/{lake}", f"{len(big_gaps)} gap(s) > 90 days (max {int(big_gaps.max())} days)")

        print(f"  {lake}: {len(df_clean)} records, {dates.min().date()} – {dates.max().date()}, missions: {df_clean['mission'].unique().tolist()}")


def audit_era5() -> None:
    print("\n=== 3. ERA5 rainfall & runoff ===")
    base = Path("./raw_data/rainfall and runoff")

    if not base.exists():
        flag("ERA5", "Directory missing — raw data not downloaded")
        return

    expected_years = np.arange(2000, 2026)
    found_years = []
    missing_years = []

    for year in expected_years:
        fpath = base / f"ERA5_{year}.nc"
        if fpath.exists():
            found_years.append(year)
        else:
            missing_years.append(year)

    if missing_years:
        flag("ERA5", f"{len(missing_years)} year file(s) missing: {missing_years}")
    print(f"  Files found: {len(found_years)}/26 expected ({found_years[0] if found_years else 'n/a'}–{found_years[-1] if found_years else 'n/a'})")

    # Spot-check the first available year
    if found_years:
        year = found_years[0]
        ds = xr.open_dataset(base / f"ERA5_{year}.nc", chunks={"valid_time": 100})
        for var in ["tp", "ro"]:
            if var not in ds:
                flag(f"ERA5/{year}", f"Variable '{var}' missing")
                continue
            data = ds[var]
            nan_frac = float(data.isnull().mean().compute())
            if nan_frac > 0.01:
                flag(f"ERA5/{year}", f"'{var}' has {100*nan_frac:.1f}% NaN values")
            neg_frac = float((data < 0).mean().compute())
            if neg_frac > 0:
                flag(f"ERA5/{year}", f"'{var}' has {100*neg_frac:.2f}% negative values")
        n_days = len(ds.valid_time)
        print(f"  Spot-check {year}: {n_days} time steps, dims: {dict(ds.dims)}")
        ds.close()


def audit_et() -> None:
    print("\n=== 4. Evapotranspiration (processed CSVs) ===")
    proc_dir = Path("./processing_data/evapotranspiration")

    if not proc_dir.exists():
        print("  Processed ET directory does not exist yet (run process_ET first).")
        return

    csv_files = list(proc_dir.glob("ET_*.csv"))
    if not csv_files:
        print("  No processed ET CSVs found.")
        return

    for fpath in sorted(csv_files):
        year_str = fpath.stem.split("_")[1]
        df = pd.read_csv(fpath, index_col=0, parse_dates=True)
        src = f"ET/{fpath.name}"

        if df.empty:
            flag(src, "Empty file")
            continue

        try:
            year = int(year_str)
        except ValueError:
            continue
        import calendar
        expected = 366 if calendar.isleap(year) else 365
        actual = len(df)
        if actual < expected:
            flag(src, f"Only {actual}/{expected} days present ({expected - actual} missing)")

        nans = df.iloc[:, 0].isna().sum()
        if nans:
            flag(src, f"{nans} NaN ET values")
        neg = (df.iloc[:, 0] < 0).sum()
        if neg:
            flag(src, f"{neg} negative ET values")

        # Values > 20 mm/day are extreme for any crop reference ET — likely a unit error
        high = (df.iloc[:, 0] > 20).sum()
        if high:
            flag(src, f"{high} ET values > 20 mm/day (check units)")

    print(f"  Processed years found: {len(csv_files)}")
    if not [i for i in ISSUES if "ET/" in i]:
        print("  No issues found.")


def audit_flood_masks() -> None:
    print("\n=== 5. Flood masks ===")
    base = Path("./raw_data/flood_masks")

    if not base.exists():
        flag("FloodMasks", "Directory missing — raw data not downloaded")
        return

    tiles = ["h20v08", "h21v08"]
    expected_years = np.arange(2000, 2026)
    types = {"recurring": base / "compact_recurring", "unusual": base / "compact_unusual"}

    for flood_type, type_dir in types.items():
        if not type_dir.exists():
            flag("FloodMasks", f"Subdirectory '{type_dir.name}' missing")
            continue

        missing = []
        for year in expected_years:
            for tile in tiles:
                fpath = type_dir / f"flood_events_{tile}_{year}.parquet"
                if not fpath.exists():
                    missing.append(f"{tile}_{year}")

        if missing:
            flag(f"FloodMasks/{flood_type}", f"{len(missing)} parquet file(s) missing (e.g. {missing[:3]})")

        sample_files = sorted(type_dir.glob("*.parquet"))
        if sample_files:
            df = pd.read_parquet(sample_files[0])
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            bad_dates = df["date"].isna().sum()
            if bad_dates:
                flag(f"FloodMasks/{flood_type}/{sample_files[0].name}", f"{bad_dates} unparseable dates")
            dupes = df.duplicated(subset=["date", "lat", "lon"]).sum()
            if dupes:
                flag(f"FloodMasks/{flood_type}/{sample_files[0].name}", f"{dupes} duplicate (date, lat, lon) entries")
            # South Sudan roughly lat 3–13, lon 24–37
            lat_out = ((df["lat"] < 3) | (df["lat"] > 13)).sum() if "lat" in df.columns else 0
            lon_out = ((df["lon"] < 24) | (df["lon"] > 37)).sum() if "lon" in df.columns else 0
            if lat_out:
                flag(f"FloodMasks/{flood_type}/{sample_files[0].name}", f"{lat_out} lat values outside South Sudan bounds [3, 13]")
            if lon_out:
                flag(f"FloodMasks/{flood_type}/{sample_files[0].name}", f"{lon_out} lon values outside South Sudan bounds [24, 37]")
            print(f"  {flood_type}: {len(sample_files)} files found, spot-check '{sample_files[0].name}': {len(df)} rows")


def audit_admin_boundaries() -> None:
    print("\n=== 6. Administrative boundaries ===")
    data_dir = Path("./raw_data/Administrative boundaries")

    if not data_dir.exists():
        flag("AdminBounds", "Directory missing")
        return

    for level, fname, name_col in [
        (1, "ssd_admin1.geojson", "adm1_name"),
        (2, "ssd_admin2.geojson", "adm2_name"),
    ]:
        fpath = data_dir / fname
        if not fpath.exists():
            flag(f"AdminBounds/L{level}", f"{fname} missing")
            continue

        gdf = gpd.read_file(fpath)
        src = f"AdminBounds/L{level}"

        if gdf.crs is None:
            flag(src, "No CRS defined")
        elif gdf.crs.to_epsg() != 4326:
            flag(src, f"CRS is {gdf.crs.to_epsg()}, expected 4326 (WGS84)")

        invalid = (~gdf.geometry.is_valid).sum()
        if invalid:
            flag(src, f"{invalid} invalid geometries")

        null_geom = gdf.geometry.isna().sum()
        if null_geom:
            flag(src, f"{null_geom} null geometries")

        if name_col not in gdf.columns:
            flag(src, f"Expected name column '{name_col}' not found — available: {gdf.columns.tolist()}")
        else:
            missing_names = gdf[name_col].isna().sum()
            if missing_names:
                flag(src, f"{missing_names} features with missing '{name_col}'")
            dupes = gdf[name_col].duplicated().sum()
            if dupes:
                flag(src, f"{dupes} duplicate '{name_col}' values")

        print(f"  Admin L{level}: {len(gdf)} features, CRS={gdf.crs.to_epsg() if gdf.crs else 'None'}")

    if not [i for i in ISSUES if "AdminBounds" in i]:
        print("  No issues found.")


def audit_worldpop() -> None:
    print("\n=== 7. WorldPop population ===")
    data_dir = Path("./raw_data/worldpop")

    if not data_dir.exists():
        flag("WorldPop", "Directory missing — raw data not downloaded")
        return

    expected_years = np.arange(2015, 2026)
    missing = []
    for year in expected_years:
        fpath = data_dir / f"ssd_pop_{year}_CN_100m_R2025A_v1.tif"
        if not fpath.exists():
            missing.append(year)

    if missing:
        flag("WorldPop", f"{len(missing)} GeoTIFF(s) missing for years: {missing}")

    found_years = [y for y in expected_years if y not in missing]
    print(f"  GeoTIFFs found: {len(found_years)}/11 expected (2015–2025)")

    if found_years:
        import rasterio
        year = found_years[0]
        fpath = data_dir / f"ssd_pop_{year}_CN_100m_R2025A_v1.tif"
        with rasterio.open(fpath) as src:
            if src.crs is None:
                flag(f"WorldPop/{year}", "No CRS")
            arr = src.read(1).astype(np.float32)
            if src.nodata is not None:
                arr[arr == src.nodata] = np.nan
            nodata_frac = np.isnan(arr).mean()
            neg = (arr < 0).sum()
            if neg:
                flag(f"WorldPop/{year}", f"{neg} negative population values")
            print(f"  Spot-check {year}: shape={arr.shape}, CRS={src.crs.to_epsg() if src.crs else 'None'}, nodata={100*nodata_frac:.1f}%")

    if not [i for i in ISSUES if "WorldPop" in i]:
        print("  No issues found.")


def audit_health_facilities() -> None:
    print("\n=== 8. Health facilities ===")
    fpath = Path("./raw_data/health facilities/Sub-Saharan_public_health_facilities.geojson")

    if not fpath.exists():
        flag("HealthFacilities", "Sub-Saharan_public_health_facilities.geojson missing")
        return

    gdf = gpd.read_file(fpath)
    gdf_ss = gdf[gdf["Country"] == "South Sudan"]

    if gdf_ss.empty:
        flag("HealthFacilities", "No features with Country == 'South Sudan' found")
        return

    src = "HealthFacilities"
    print(f"  South Sudan facilities: {len(gdf_ss)} (of {len(gdf)} total sub-Saharan)")

    null_geom = gdf_ss.geometry.isna().sum()
    if null_geom:
        flag(src, f"{null_geom} facilities with null geometry")

    invalid = (~gdf_ss.geometry.is_valid).sum()
    if invalid:
        flag(src, f"{invalid} invalid geometries")

    # Coordinates inside South Sudan bbox — rough bounds lat 3–13, lon 24–37
    coords = gdf_ss.geometry[gdf_ss.geometry.notna()]
    lons = coords.x
    lats = coords.y
    out_bounds = ((lats < 3) | (lats > 13) | (lons < 24) | (lons > 37)).sum()
    if out_bounds:
        flag(src, f"{out_bounds} facilities with coordinates outside South Sudan bounds")

    if "Facility_n" in gdf_ss.columns:
        dupes = gdf_ss["Facility_n"].duplicated().sum()
        if dupes:
            flag(src, f"{dupes} duplicate facility names")

    if "Type" in gdf_ss.columns:
        expected_types = {"PHCU", "PHCC", "State Hospital", "Teaching Hospital", "County Hospital"}
        actual_types = set(gdf_ss["Type"].dropna().unique())
        unknown_types = actual_types - expected_types
        if unknown_types:
            flag(src, f"Unexpected facility types: {unknown_types}")
        print(f"  Facility types: {gdf_ss['Type'].value_counts().to_dict()}")

    if not [i for i in ISSUES if "HealthFacilities" in i]:
        print("  No issues found.")


def audit_ipc() -> None:
    print("\n=== 9. IPC food insecurity ===")
    folder = Path("./raw_data/IPC")

    if not folder.exists():
        flag("IPC", "Directory missing — raw data not downloaded")
        return

    fnames = [f for f in os.listdir(folder) if f.endswith(".xlsx")]
    if not fnames:
        flag("IPC", "No .xlsx files found")
        return

    all_counties: list[pd.DataFrame] = []
    for filename in fnames:
        filepath = folder / filename
        df = pd.read_excel(filepath)

        if "Area Name" not in df.columns:
            flag(f"IPC/{filename}", "Column 'Area Name' not found")
            continue
        if "Current - Phase 3+" not in df.columns:
            flag(f"IPC/{filename}", "Column 'Current - Phase 3+' not found")
            continue

        df["is_county"] = df["Area Name"].str.startswith("  ")
        df["Area Name"] = df["Area Name"].str.strip()
        counties = df[df["is_county"]].copy()

        if counties.empty:
            flag(f"IPC/{filename}", "No county rows detected (no rows with leading spaces in 'Area Name')")
            continue

        for date_col in ["Current - From Date", "Current - Thru Date"]:
            if date_col in counties.columns:
                # Dates are stored as Excel serial numbers
                counties[date_col] = pd.to_datetime(counties[date_col], unit="D", errors="coerce")
                bad = counties[date_col].isna().sum()
                if bad:
                    flag(f"IPC/{filename}", f"{bad} unparseable '{date_col}' values")

        phase_col = "Current - Phase 3+"
        neg = (counties[phase_col] < 0).sum() if pd.api.types.is_numeric_dtype(counties[phase_col]) else 0
        if neg:
            flag(f"IPC/{filename}", f"{neg} negative Phase 3+ population values")

        nans = counties[phase_col].isna().sum()
        if nans:
            flag(f"IPC/{filename}", f"{nans} NaN Phase 3+ values ({100*nans/len(counties):.0f}%)")

        all_counties.append(counties[["Area Name", phase_col]].rename(columns={"Area Name": "County"}))
        print(f"  {filename}: {len(counties)} counties")

    # Cross-file county name consistency check
    if len(all_counties) >= 2:
        county_sets = [set(df["County"].dropna()) for df in all_counties]
        base_set = county_sets[0]
        for i, cs in enumerate(county_sets[1:], 1):
            only_in_base = base_set - cs
            only_in_other = cs - base_set
            if only_in_base or only_in_other:
                flag("IPC/cross-file", f"File {i+1} has county name mismatches: "
                     f"{len(only_in_base)} counties only in file 1, {len(only_in_other)} only in file {i+1}")

    if not [i for i in ISSUES if "IPC" in i]:
        print("  No issues found.")


def audit_gdp() -> None:
    print("\n=== 10. GDP (World Bank) ===")
    fpath = Path("./raw_data/GDP/API_SSD_DS2_en_csv_v2_2529.csv")

    if not fpath.exists():
        flag("GDP", "API_SSD_DS2_en_csv_v2_2529.csv missing")
        return

    data = pd.read_csv(fpath, skiprows=4)
    gdp = data[data["Indicator Name"] == "GDP (current US$)"]

    if gdp.empty:
        flag("GDP", "No row found with Indicator Name == 'GDP (current US$)'")
        return

    # Check documented available range 2008–2015
    expected_years = list(range(2008, 2016))
    missing_years = [y for y in expected_years if str(y) not in gdp.columns or pd.isna(gdp[str(y)].values[0])]
    if missing_years:
        flag("GDP", f"GDP missing for documented years: {missing_years}")

    year_cols = [c for c in gdp.columns if c.isdigit()]
    vals = gdp[year_cols].iloc[0].dropna().astype(float)
    neg = (vals < 0).sum()
    if neg:
        flag("GDP", f"{neg} year(s) with negative GDP")

    present = [y for y in expected_years if str(y) in gdp.columns and not pd.isna(gdp[str(y)].values[0])]
    print(f"  GDP available years: {present}")

    if not [i for i in ISSUES if "GDP" in i]:
        print("  No issues found.")


# Audit registry — name and auditor function, mirrors the DATASETS registry in data_cleaning.py
AUDITS = [
    {"name": "Dartmouth Flood Observatory",  "auditor": audit_dartmouth},
    {"name": "Lake water levels",            "auditor": audit_lakes},
    {"name": "ERA5 rainfall & runoff",       "auditor": audit_era5},
    {"name": "Evapotranspiration",           "auditor": audit_et},
    {"name": "Flood masks",                  "auditor": audit_flood_masks},
    {"name": "Administrative boundaries",    "auditor": audit_admin_boundaries},
    {"name": "WorldPop population",          "auditor": audit_worldpop},
    {"name": "Health facilities",            "auditor": audit_health_facilities},
    {"name": "IPC food insecurity",          "auditor": audit_ipc},
    {"name": "GDP",                          "auditor": audit_gdp},
]


# Run — calls each auditor in order
def run_audit(entry: dict) -> None:
    entry["auditor"]()


def print_summary() -> None:
    print("\n" + "=" * 60)
    print(f"AUDIT SUMMARY: {len(ISSUES)} issue(s) found")
    print("=" * 60)
    if ISSUES:
        for i, issue in enumerate(ISSUES, 1):
            print(f"  {i:3d}. {issue}")
    else:
        print("  All checked datasets appear clean.")
    print()


# Main
if __name__ == "__main__":
    print("Starting data quality audit...")
    for entry in AUDITS:
        run_audit(entry)
    print_summary()
