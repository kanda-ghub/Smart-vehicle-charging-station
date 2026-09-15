import click
from flask import Blueprint
from .pv_forecaster import run_irradiance_to_pv_forecast

# Register a custom CLI group named 'pv'
__plugin_blueprint__ = Blueprint("pv_forecast_data_plugin", __name__, cli_group="pv")

@__plugin_blueprint__.cli.command("forecast")
@click.option("--sensor", required=True, type=int, help="Target PV sensor ID")
@click.option("--irradiance-sensor", required=True, type=int, help="Source irradiance sensor ID")
@click.option("--from-date", required=True, help="Start timestamp (ISO format)")
@click.option("--to-date", required=True, help="End timestamp (ISO format)")
@click.option("--capacity", default=40.0, type=float, help="Capacity in kWp")
def forecast_command(sensor, irradiance_sensor, from_date, to_date, capacity):
    """Generate PV power forecasts from irradiance data."""
    run_irradiance_to_pv_forecast(
        target_sensor_id=sensor,
        irradiance_sensor_id=irradiance_sensor,
        start_str=from_date,
        end_str=to_date,
        capacity_kwp=capacity
    )
    print(f"Successfully generated PV forecasts for sensor {sensor}!")

def __init_app__(app):
    """
    Hook called during FlexMeasures app creation.
    """
    pass
