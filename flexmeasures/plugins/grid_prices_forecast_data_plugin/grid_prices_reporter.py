import os
import click
import pandas as pd
from datetime import timedelta, timezone
from marshmallow import fields, INCLUDE, validates_schema
from flask import current_app
from entsoe import EntsoePandasClient

from flexmeasures.data.models.reporting.pandas_reporter import (
    PandasReporter,
    PandasReporterParametersSchema,
    PandasReporterConfigSchema,
)
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.data.services.data_sources import get_or_create_source
from flexmeasures.data.config import db

from timely_beliefs import BeliefsDataFrame


class GridPriceConfigSchema(PandasReporterConfigSchema):
    input = fields.List(fields.Dict(), load_default=[])
    output = fields.List(fields.Dict(), required=True)
    
    multiplier = fields.Float(load_default=1.2)
    adder = fields.Float(load_default=35.0)
    transformations = fields.List(fields.Dict(), load_default=[])

    class Meta:
        unknown = INCLUDE

    @validates_schema
    def validate_chaining(self, data, **kwargs):
        if not data:
            return
        if data.get("transformations") is None:
            data["transformations"] = []
        if data.get("required_input") is None:
            data["required_input"] = []
        if data.get("output") is None:
            data["output"] = []

        if data.get("transformations"):
            try:
                super().validate_chaining(data, **kwargs)
            except Exception:
                pass


class GridPriceParametersSchema(PandasReporterParametersSchema):
    input = fields.List(fields.Dict(), load_default=[])

    class Meta:
        unknown = INCLUDE


class EntsoePriceReporter(PandasReporter):
    """
    Fetches wholesale day-ahead market clearing prices (MCP in EUR/MWh)
    directly from the ENTSO-E API for Greece and transforms them 
    into Protergia Dynamic One tariff prices (€/kWh).
    Formula: (1.2 * MCP_EUR_MWh + 35) / 1000
    """

    _config_schema = GridPriceConfigSchema()
    _parameters_schema = GridPriceParametersSchema()

    def _fetch_from_entsoe(self, start, end, target_tz) -> pd.DataFrame:
        """Fetches day-ahead market prices directly from ENTSO-E API for Greece."""
        api_key = current_app.config.get("ENTSOE_API_KEY") or os.getenv("ENTSOE_API_KEY")
        if not api_key:
            raise ValueError("ENTSOE_API_KEY is missing from Flask config and environment variables.")

        client = EntsoePandasClient(api_key=api_key)
        
        start_ts = pd.Timestamp(start)
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize(target_tz)
        else:
            start_ts = start_ts.tz_convert(target_tz)

        end_ts = pd.Timestamp(end)
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize(target_tz)
        else:
            end_ts = end_ts.tz_convert(target_tz)

        click.echo(f"-> Fetching ENTSO-E data for Greece from {start_ts} to {end_ts}...")
        ts_wholesale = client.query_day_ahead_prices('GR', start=start_ts, end=end_ts)
        
        df = ts_wholesale.to_frame(name='mcp_eur_mwh').reset_index()
        df.rename(columns={'index': 'event_start'}, inplace=True)
        df.set_index('event_start', inplace=True)
        return df

    def _unwrap_dataframe(self, obj) -> pd.DataFrame | None:
        if isinstance(obj, pd.DataFrame):
            return obj
        if isinstance(obj, list) and len(obj) > 0:
            return self._unwrap_dataframe(obj[0])
        if isinstance(obj, dict) and len(obj) > 0:
            return self._unwrap_dataframe(next(iter(obj.values())))
        return None

    def _compute(
        self,
        input_data: list[pd.DataFrame] | dict[str, pd.DataFrame] | None = None,
        **kwargs
    ) -> list[dict] | pd.DataFrame:

        start, end = kwargs.get("start"), kwargs.get("end")
        config = getattr(self, "_config", {}) or {}

        # 1. Target Output Sensor
        output_list = config.get("output", [])
        if not output_list or "sensor" not in output_list[0]:
            raise ValueError("Reporter configuration missing 'output' sensor ID.")

        output_sensor_id = output_list[0]["sensor"]
        target_sensor = db.session.get(Sensor, output_sensor_id)
        if not target_sensor:
            raise ValueError(f"Output sensor with ID {output_sensor_id} not found.")

        target_tz = getattr(target_sensor, "timezone", "Europe/Athens") or "Europe/Athens"

        # 2. Extract Formula Parameters
        multiplier = config.get("multiplier", 1.2)
        adder = config.get("adder", 35.0)

        # 3. Retrieve Wholesale MCP Data (API fetch prioritized)
        df = None
        try:
            df = self._fetch_from_entsoe(start, end, target_tz)
        except Exception as api_err:
            click.echo(f"Notice: Live ENTSO-E fetch failed ({api_err}). Checking fallback inputs...")
            
            raw_data = input_data if input_data is not None else kwargs.get("input_data")
            if raw_data is not None:
                df = self._unwrap_dataframe(raw_data)

            input_list = config.get("input", [])
            if (df is None or df.empty) and input_list and "sensor" in input_list[0]:
                input_sensor = db.session.get(Sensor, input_list[0]["sensor"])
                if input_sensor:
                    bdf = TimedBelief.search(sensors=[input_sensor], event_starts_after=start, event_ends_before=end)
                    if not bdf.empty:
                        df = pd.DataFrame(bdf)

        if df is None or df.empty:
            raise ValueError("Error: Failed to fetch data from ENTSO-E API and no valid fallback input data found.")

        # Enforce timezone alignment on index
        if df.index.tz is None:
            df.index = df.index.tz_localize(target_tz)
        else:
            df.index = df.index.tz_convert(target_tz)

        # 4. Filter data within scope bounds localized to target timezone
        if start is not None:
            start_tz = pd.to_datetime(start).tz_convert(target_tz)
            df = df[df.index >= start_tz]
        if end is not None:
            end_tz = pd.to_datetime(end).tz_convert(target_tz)
            df = df[df.index <= end_tz]

        if df.empty:
            click.echo("Warning: Data retrieved, but none fell within requested report time window.")
            return pd.DataFrame()

        # 5. Apply Protergia Dynamic One Formula: (1.2 * MCP_EUR_MWh + 35) / 1000
        mcp_eur_mwh = df.iloc[:, 0]
        net_price_kwh = ((multiplier * mcp_eur_mwh + adder) / 1000.0).round(5)

        # 6. Construct TimelyBeliefs DataFrame
        reporter_source = get_or_create_source(
            source=self.__class__.__name__,
            source_type="reporter",
        )

        event_starts = df.index
        now_utc = pd.Timestamp.now(timezone.utc)
        n_rows = len(net_price_kwh)

        flat_df = pd.DataFrame({
            "event_start": event_starts,
            "event_value": net_price_kwh.values,
            "belief_time": [now_utc] * n_rows,
            "source": [reporter_source] * n_rows,
            "cumulative_probability": [0.5] * n_rows,
        })

        b_df = BeliefsDataFrame(flat_df, sensor=target_sensor)

        click.echo("\n=================== REPORTERS OUTPUT ===================")
        click.echo(f"Target Price Sensor ID: {target_sensor.id} ({target_sensor.name})")
        click.echo(f"Sensor Timezone: {target_tz}")
        click.echo(f"Applied Formula: ({multiplier} * MCP_EUR_MWh + {adder}) / 1000")
        click.echo(f"Parsed records count: {len(b_df)}")
        if not b_df.empty:
            click.echo(f"Min Price: €{b_df['event_value'].min():.5f} / kWh")
            click.echo(f"Max Price: €{b_df['event_value'].max():.5f} / kWh")
            click.echo("\nFirst 5 parsed Protergia Dynamic records (€/kWh):")
            click.echo(b_df.head(5))
        click.echo("========================================================\n")

        return [{"data": b_df, "sensor": target_sensor}]
