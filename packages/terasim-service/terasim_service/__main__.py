import uvicorn
from uvicorn.config import LOGGING_CONFIG

from terasim_service.api import create_app
from terasim_service.city.common import city_log_config


def main():
    # Create the FastAPI application
    app = create_app()

    # Run the application with uvicorn. Its logging config also shows the city
    # pipeline's INFO lines (warm starts, skipped scheduler ticks), which uvicorn's
    # default leaves out.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=city_log_config(LOGGING_CONFIG))


if __name__ == "__main__":
    main()
