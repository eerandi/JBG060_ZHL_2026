"""
Data cleaning pipeline.
Loads every dataset, removes NaN values, and duplicates and flood data outside of South sudan and saves cleaned outputs to ./cleaned_data/.
"""
import io
import os
import warnings
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import geopandas as gpd
import xarray as xr

warnings.filterwarnings("ignore")

OUT_ROOT = Path("./cleaned_data")



#cleaners — each returns a cleaned object of the same type as the input
def clean_dataframe(df: pd.DataFrame, date_col: str = None, dedup_cols: list = None,
                    dropna_subset: list = None) -> pd.DataFrame:
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=dropna_subset)
    # Filter dedup_cols to columns present in this DataFrame
    valid_dedup = [c for c in dedup_cols if c in df.columns] if dedup_cols else None
    df = df.drop_duplicates(subset=valid_dedup)
    if date_col and date_col in df.columns:
        df = df.set_index(date_col).sort_index()
    return df


def clean_geodataframe(gdf: gpd.GeoDataFrame, required_cols: list = None) -> gpd.GeoDataFrame:
    gdf = gdf[gdf.geometry.notna() & gdf.geometry.is_valid]
    if required_cols:
        present = [c for c in required_cols if c in gdf.columns]
        gdf = gdf.dropna(subset=present)
    return gdf


def clean_netcdf(ds: xr.Dataset, variables: list = None, fill_value: float = 0.0) -> xr.Dataset:
    # Fill rather than drop because dropping cells breaks the spatial grid structure
    target = ds[variables] if variables else ds
    return target.fillna(fill_value)


# Save function — dispatches by object type
def save(obj, rel_path: str) -> None:
    out = OUT_ROOT / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, gpd.GeoDataFrame):
        obj.to_file(out, driver="GeoJSON")
        print(f"  Saved {len(obj)} features -> {out}")
    elif isinstance(obj, xr.Dataset):
        # Compress each variable to reduce file size and write time
        encoding = {v: {"zlib": True, "complevel": 4} for v in obj.data_vars}
        obj.to_netcdf(out, encoding=encoding)
        print(f"  Saved NetCDF -> {out}")
    elif isinstance(obj, pd.DataFrame):
        obj.to_parquet(out, index=False, engine="pyarrow") if str(out).endswith(".parquet") else obj.to_csv(out)
        print(f"  Saved {len(obj)} rows -> {out}")


# Loaders — each returns a list of (data_object, output_rel_path) tuples
def load_dartmouth() -> list:
    base = Path("./raw_data/Darthmouth Flood Observatory")
    if not (base / "information.xlsx").exists():
        return []
    info = pd.read_excel(base / "information.xlsx")
    results = []
    for area_id in info["area id"].values:
        fpath = base / f"{area_id}_discharge.csv"
        if fpath.exists():
            results.append((pd.read_csv(fpath), f"dartmouth/{fpath.name}"))
        else:
            print(f"  WARNING: {fpath.name} not found")
    return results


def load_lakes() -> list:
    base = Path("./raw_data/Water levels lakes")
    if not base.exists():
        return []
    results = []

    albert = base / "water_level_altimetry_Albert.nc"
    if albert.exists():
        ds = xr.open_dataset(albert)
        df = ds.to_dataframe().reset_index()
        ds.close()
        # Albert uses "datetime" column — rename to "date" to match Victoria/Kyoga
        time_col = "datetime" if "datetime" in df.columns else "time"
        df = df.rename(columns={time_col: "date"})
        results.append((df, "lakes/Albert_water_level.csv"))

    mission_names = {"TOPEX", "JASON-1", "JASON-2", "JASON-3", "J1", "J2",
                     "J3", "S3A", "S3B", "S6A", "ENVISAT", "ERS-1", "ERS-2"}
    col_names = ["mission", "cycle", "date", "hour", "minute", "height_wrt_ref",
                 "height_err", "backscatter_ku", "wet_tropo_corr", "iono_corr",
                 "dry_tropo_corr", "mode1", "mode2", "ice_flag", "height_egm2008", "data_source_flag"]
    for lake in ["victoria", "Kyoga"]:
        fpath = base / f"water_level_{lake}.txt"
        if not fpath.exists():
            print(f"  WARNING: {fpath.name} not found"); continue
        lines = []
        with open(fpath, "r", encoding="latin-1") as f:
            for line in f:
                token = line.strip().split()[0] if line.strip() else ""
                if token in mission_names:
                    lines.append(line.strip())
        df = pd.read_csv(io.StringIO("\n".join(lines)), sep=r"\s+", names=col_names,
                         na_values=["999.99", "99.999", "9999.99"])
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce")
        results.append((df, f"lakes/{lake}_water_level.csv"))
    return results


def _era5_worker(fpath: Path) -> None:
    out = OUT_ROOT / f"era5/{fpath.name}"
    out.parent.mkdir(parents=True, exist_ok=True)
    ds = xr.open_dataset(fpath, chunks={"valid_time": 365})
    ds_daily = ds[["tp", "ro"]].resample(valid_time="1D").sum().fillna(0.0)
    encoding = {v: {"zlib": True, "complevel": 1} for v in ds_daily.data_vars}
    ds_daily.to_netcdf(out, encoding=encoding)
    ds.close()
    print(f"  Saved {fpath.stem}")


def load_era5() -> list:
    base = Path("./raw_data/rainfall and runoff")
    if not base.exists():
        return []
    # Skip files whose cleaned output already exists
    files = [f for f in sorted(base.glob("ERA5_*.nc"))
             if not (OUT_ROOT / f"era5/{f.name}").exists()]
    if not files:
        print("  All ERA5 files already processed, skipping")
        return []
    n_workers = 4
    print(f"  Processing {len(files)} file(s) with {n_workers} parallel workers...")
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        list(executor.map(_era5_worker, files))
    return []  # already saved by workers


def load_et() -> list:
    proc_dir = Path("./processing_data/evapotranspiration")
    if not proc_dir.exists():
        return []
    return [(pd.read_csv(f, index_col=0, parse_dates=True), f"evapotranspiration/{f.name}")
            for f in sorted(proc_dir.glob("ET_*.csv"))]


def load_flood_masks() -> list:
    base = Path("./raw_data/flood_masks")
    if not base.exists():
        return []

    # Load the national border once and reuse for all files to filter out-of-boundary points
    ssd = gpd.read_file("./raw_data/Administrative boundaries/ssd_admin0.geojson")

    results = []
    for subdir_name in ["compact_recurring", "compact_unusual"]:
        subdir = base / subdir_name
        if not subdir.exists():
            print(f"  WARNING: {subdir_name} not found"); continue
        for fpath in sorted(subdir.glob("*.parquet")):
            df = pd.read_parquet(fpath, engine="pyarrow")
            before = len(df)

            # Build point geometries and keep only those inside the actual South Sudan border
            gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")
            inside = gpd.sjoin(gdf, ssd[["geometry"]], how="inner", predicate="within")
            df = df.loc[inside.index].reset_index(drop=True)

            removed = before - len(df)
            if removed:
                print(f"  {fpath.name}: removed {removed} rows outside South Sudan border")
            results.append((df, f"flood_masks/{subdir_name}/{fpath.name}"))
    return results


def load_admin_boundaries() -> list:
    data_dir = Path("./raw_data/Administrative boundaries")
    if not data_dir.exists():
        return []
    return [(gpd.read_file(data_dir / fname), f"administrative_boundaries/{fname}")
            for fname in ["ssd_admin1.geojson", "ssd_admin2.geojson"]
            if (data_dir / fname).exists()]


def load_health_facilities() -> list:
    fpath = Path("./raw_data/health facilities/Sub-Saharan_public_health_facilities.geojson")
    if not fpath.exists():
        return []
    gdf = gpd.read_file(fpath)
    gdf = gdf[gdf["Country"] == "South Sudan"].copy()

    # Keep the most complete record when facility names are duplicated
    if "Facility_n" in gdf.columns:
        gdf["_non_null"] = gdf.notna().sum(axis=1)
        gdf = (gdf.sort_values("_non_null", ascending=False)
                  .drop_duplicates(subset=["Facility_n"])
                  .drop(columns=["_non_null"])
                  .reset_index(drop=True))

    return [(gdf, "health_facilities/south_sudan_health_facilities.geojson")]


def load_ipc() -> list:
    folder = Path("./raw_data/IPC")
    if not folder.exists():
        return []
    parts = []
    for fname in sorted(f for f in os.listdir(folder) if f.endswith(".xlsx")):
        df = pd.read_excel(folder / fname)
        if "Area Name" not in df.columns or "Current - Phase 3+" not in df.columns:
            print(f"  WARNING: {fname} missing expected columns, skipping"); continue
        df["is_county"] = df["Area Name"].str.startswith("  ")
        df["Area Name"] = df["Area Name"].str.strip()
        counties = df[df["is_county"]].copy().rename(columns={"Area Name": "County"})
        # Normalise county names — title-case + collapsed spaces prevents duplicate
        # columns in the pivot when the same county is spelled differently across files
        counties["County"] = (counties["County"]
                              .str.strip()
                              .str.title()
                              .str.replace(r"\s+", " ", regex=True))
        for col in ["Current - From Date", "Current - Thru Date"]:
            if col in counties.columns:
                # Dates are stored as Excel serial numbers
                counties[col] = pd.to_datetime(counties[col], unit="D", errors="coerce")
        keep = [c for c in ["Current - From Date", "Current - Thru Date", "County", "Current - Phase 3+"]
                if c in counties.columns]
        parts.append(counties[keep])
    if not parts:
        return []
    combined = pd.concat(parts, ignore_index=True).rename(columns={
        "Current - From Date": "Start Date", "Current - Thru Date": "End Date",
        "Current - Phase 3+": "Phase 3+ Pop",
    })
    result = (combined.pivot_table(index=["Start Date", "End Date"], columns="County", values="Phase 3+ Pop")
              .reset_index().sort_values("Start Date").reset_index(drop=True))
    return [(result, "ipc/ipc_phase3plus.csv")]


def load_gdp() -> list:
    fpath = Path("./raw_data/GDP/API_SSD_DS2_en_csv_v2_2529.csv")
    if not fpath.exists():
        return []
    data = pd.read_csv(fpath, skiprows=4)
    gdp = data[data["Indicator Name"] == "GDP (current US$)"]
    if gdp.empty:
        return []
    year_cols = [c for c in gdp.columns if c.isdigit()]
    gdp_series = gdp[year_cols].T.rename(columns={gdp.index[0]: "GDP_USD"})
    gdp_series.index = gdp_series.index.astype(int)
    gdp_series.index.name = "Year"
    return [(gdp_series, "gdp/south_sudan_gdp.csv")]



# Dataset registry — name, loader, cleaner, and cleaner kwargs
DATASETS = [
    {"name": "Dartmouth discharge",    "loader": load_dartmouth,        "clean": clean_dataframe,    "kwargs": {"date_col": "Date",        "dedup_cols": ["Date"]}},
    {"name": "Lake water levels",      "loader": load_lakes,            "clean": clean_dataframe,    "kwargs": {"date_col": "date",        "dedup_cols": ["date", "mission"]}},
    {"name": "ERA5 rainfall & runoff", "loader": load_era5,             "clean": clean_netcdf,       "kwargs": {"variables": ["tp", "ro"], "fill_value": 0.0}},
    {"name": "Evapotranspiration",     "loader": load_et,               "clean": clean_dataframe,    "kwargs": {}},
    {"name": "Flood masks",            "loader": load_flood_masks,      "clean": clean_dataframe,    "kwargs": {"date_col": "date",        "dedup_cols": ["date", "lat", "lon"]}},
    {"name": "Administrative bounds",  "loader": load_admin_boundaries, "clean": clean_geodataframe, "kwargs": {"required_cols": ["adm1_name", "adm2_name"]}},
    {"name": "Health facilities",      "loader": load_health_facilities,"clean": clean_geodataframe, "kwargs": {"required_cols": ["Facility_n", "Type"]}},
    {"name": "IPC food insecurity",    "loader": load_ipc,              "clean": clean_dataframe,    "kwargs": {}},
    {"name": "GDP",                    "loader": load_gdp,              "clean": clean_dataframe,    "kwargs": {}},
]



# Process — identical for every dataset: load → clean → save
def process(dataset: dict) -> None:
    print(f"\n=== {dataset['name']} ===")
    items = dataset["loader"]()
    if items is None:
        print("  Skipping — data not found")
        return
    if not items:
        return  # loader handled its own processing (e.g. ERA5 parallel workers)
    for obj, out_path in items:
        cleaned = dataset["clean"](obj, **dataset["kwargs"])
        save(cleaned, out_path)



# Main
if __name__ == "__main__":
    print(f"Output directory: {OUT_ROOT.resolve()}")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    for dataset in DATASETS:
        process(dataset)

    print("\nDone. Cleaned files saved in ./cleaned_data/")
