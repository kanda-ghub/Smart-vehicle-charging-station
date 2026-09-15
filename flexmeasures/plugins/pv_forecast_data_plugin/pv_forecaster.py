import pandas as pd
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.data.models.data_sources import DataSource
from flexmeasures.data.config import db


def run_irradiance_to_pv_forecast(
    target_sensor_id: int,
    irradiance_sensor_id: int,
    start_str: str,
    end_str: str,
    capacity_kwp: float = 40.0,
    derating_factor: float = 0.85,
    forecaster_name: str = "IrradianceToPVForecaster"
):
    start = pd.Timestamp(start_str)
    end = pd.Timestamp(end_str)

    target_sensor = db.session.get(Sensor, target_sensor_id)
    irradiance_sensor = db.session.get(Sensor, irradiance_sensor_id)

    if not target_sensor or not irradiance_sensor:
        raise ValueError("Target or irradiance sensor not found in database.")

    # Retrieve or create a DataSource representing the forecaster
    data_source = DataSource.query.filter_by(name=forecaster_name, type="forecaster").first()
    if not data_source:
        data_source = DataSource(name=forecaster_name, type="forecaster")
        db.session.add(data_source)
        db.session.flush()

    # Fetch the most recent irradiance beliefs for the specified period
    irradiance_df = irradiance_sensor.search_beliefs(
        event_starts_after=start,
        event_ends_before=end,
        most_recent_only=False
    )

    if irradiance_df.empty:
        print("No irradiance beliefs found for the specified period.")
        return

    # Handle MultiIndex or standard index if present
    if isinstance(irradiance_df.index, pd.MultiIndex):
        irradiance_df = irradiance_df.reset_index()

    # Locate event_start and value columns safely
    time_col = "event_start" if "event_start" in irradiance_df.columns else irradiance_df.columns[0]
    val_col = "observation" if "observation" in irradiance_df.columns else ("value" if "value" in irradiance_df.columns else irradiance_df.columns[-1])

    # Construct clean time-indexed series
    irr_series = pd.Series(
        irradiance_df[val_col].values,
        index=pd.to_datetime(irradiance_df[time_col])
    ).sort_index()

    # Drop any duplicate timestamps if they exist
    irr_series = irr_series[~irr_series.index.duplicated(keep="first")]

    # Resample and linearly interpolate to match target sensor resolution (e.g., 15 mins)
    target_resolution = target_sensor.event_resolution
    irr_resampled = irr_series.resample(target_resolution).interpolate(method="linear")

    # Convert irradiance (W/m²) to PV power (kW)
    pv_power = irr_resampled * (capacity_kwp / 1000.0) * derating_factor
    pv_power = pv_power.clip(lower=0.0)

    # Save beliefs with proper belief times to ensure positive horizon
    for event_start, value in pv_power.items():
        # Set belief time 1 hour before the event start so it registers as a valid forecast
        belief_time = event_start - pd.Timedelta(hours=1)
        
        belief = TimedBelief(
            event_start=event_start,
            belief_time=belief_time,
            event_value=float(value),
            cumulative_probability=0.5,
            sensor=target_sensor,
            source=data_source
        )
        db.session.merge(belief)

    db.session.commit()
    print(f"Successfully generated and saved PV forecasts for sensor {target_sensor_id} ({len(pv_power)} intervals processed).")
