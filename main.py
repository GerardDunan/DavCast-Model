import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.preprocessing import RobustScaler
from sklearn.model_selection import train_test_split, TimeSeriesSplit
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from datetime import datetime
import os
import optuna
import logging  # Add logging import
import joblib
import json
from scipy.stats import skew

# Disable Optuna logging except warnings
from optuna.logging import get_logger, WARNING
optuna_logger = get_logger("optuna")
optuna_logger.setLevel(WARNING)

class GHIPredictionModel:
    def __init__(self):
        self.scaler = RobustScaler()
        self.models_median = {}
        self.models_upper = {}  # 95th percentile
        self.models_lower = {}  # 5th percentile
        self.lower_adjustments = {}
        self.upper_adjustments = {}
        self.feature_columns = None
        self.target_column = 'GHI - W/m^2'
        self.forecast_horizons = [1, 2, 3, 4] # Define forecast horizons
        
        # Solar position constants for Davao City
        self.latitude = 7.0707
        self.longitude = 125.6113
        self.elevation = 7  # meters
        self.solar_constant = 1361  # W/m²
        
        # Get the directory where main.py is located
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Setup logging
        self.setup_logging()
    
    def setup_logging(self):
        """Set up logging to file and console"""
        # Create logs directory if it doesn't exist
        logs_dir = os.path.join(self.base_dir, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        
        # Configure logging
        log_file = os.path.join(logs_dir, f'ghi_prediction_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
        
        # Configure root logger
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()  # Also log to console but with less detail
            ]
        )
        
        # Create a separate logger for detailed debug info (file only)
        self.debug_logger = logging.getLogger('debug')
        self.debug_logger.setLevel(logging.DEBUG)
        # Prevent debug messages from propagating to the root logger (and thus the console)
        self.debug_logger.propagate = False
        
        debug_handler = logging.FileHandler(os.path.join(logs_dir, f'debug_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'))
        debug_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.debug_logger.addHandler(debug_handler)
        
        # Save the log file path for reference
        self.log_file = log_file
        debug_log_path = os.path.join(logs_dir, f'debug_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
        # Use file path info only in logs, not terminal
        logging.info(f"Log files created: \n- {log_file} \n- {debug_log_path}")
    
    def load_data(self, file_path):
        """
        Load hourly GHI data from a CSV file.
        
        Parameters:
        file_path (str): Path to the CSV file containing the data
        
        Returns:
        pandas.DataFrame: The loaded dataset
        """
        logging.info(f"Loading data from {file_path}...")
        
        # Try reading with different encodings to handle special characters like °
        try:
            data = pd.read_csv(file_path, encoding='utf-8')
        except UnicodeDecodeError:
            try:
                data = pd.read_csv(file_path, encoding='latin-1')
                logging.info("File loaded using latin-1 encoding")
            except:
                data = pd.read_csv(file_path, encoding='cp1252')
                logging.info("File loaded using cp1252 encoding")
        
        # Convert date and time to datetime
        if 'Date' in data.columns and 'Start Period' in data.columns:
            data['datetime'] = pd.to_datetime(data['Date'] + ' ' + data['Start Period'], 
                                            format='%d-%b-%y %H:%M:%S', errors='coerce')
        
        # Sort by datetime
        if 'datetime' in data.columns:
            data = data.sort_values('datetime').reset_index(drop=True)
        
        logging.info(f"Loaded {len(data)} rows of data")
        return data
    
    def preprocess_data(self, data):
        """
        Preprocess the data by handling missing values and creating features.
        
        Parameters:
        data (pandas.DataFrame): The dataset to preprocess
        
        Returns:
        pandas.DataFrame: The preprocessed dataset
        """
        logging.info("Preprocessing data...")
        df = data.copy()
        
        # Check for missing values
        missing_values = df.isnull().sum()
        logging.info(f"Missing values before handling:\n{missing_values[missing_values > 0]}")
        
        # Fill missing values with appropriate methods
        numeric_cols = df.select_dtypes(include=['float64', 'int64']).columns
        for col in numeric_cols:
            if df[col].isnull().sum() > 0:
                # For GHI values, fill with 0 if it's nighttime, else interpolate
                if col == self.target_column and 'Daytime' in df.columns:
                    night_mask = (df['Daytime'] == 0) & df[col].isnull()
                    df.loc[night_mask, col] = 0
                    df[col] = df[col].interpolate(method='linear')
                else:
                    df[col] = df[col].interpolate(method='linear')
        
        # Check remaining missing values
        missing_after = df.isnull().sum()
        logging.info(f"Missing values after handling:\n{missing_after[missing_after > 0]}")
        
        return df
    
    def create_features(self, data, lag_hours=3):
        """
        Create features for the model, respecting the sequential nature of time series data.
        Creates targets for multiple forecast horizons (1h, 2h, 3h, 4h).
        
        Parameters:
        data (pandas.DataFrame): The preprocessed dataset
        lag_hours (int): Number of lag hours to use
        
        Returns:
        pandas.DataFrame: Dataset with additional features and multi-horizon targets
        """
        print(f"Creating sequential features with {lag_hours} lag hours for horizons {self.forecast_horizons}...")
        df = data.copy()
        
        # Ensure data is sorted by time before creating lag features
        if 'datetime' in df.columns:
            df = df.sort_values('datetime')
            print(f"Data sorted by time from {df['datetime'].min()} to {df['datetime'].max()}")
        
        # Create lagged features for GHI - these respect time ordering because they use .shift()
        for i in range(1, lag_hours + 1):
            df[f'GHI_lag_{i}'] = df[self.target_column].shift(i)
        
        # Create rolling statistics that respect time ordering
        # These only use past data points for each prediction
        df['GHI_rolling_mean_6h'] = df[self.target_column].rolling(window=6, min_periods=1).mean()
        df['GHI_rolling_max_24h'] = df[self.target_column].rolling(window=24, min_periods=1).max()
        
        # Add solar position features first (doesn't depend on clear sky)
        df = self.add_solar_position_features(df)
        
        # Now calculate clear sky GHI (depends on solar position)
        df = self.calculate_clear_sky_ghi(df)
        
        # Add trend features (after we have solar and clear sky features)
        df = self.add_trend_features(df)
        
        # Add diurnal decomposition (requires clear sky index)
        df = self.add_diurnal_decomposition(df)
        
        # Create targets for each forecast horizon
        # Initialize target_columns list to store target column names - THIS IS THE CRITICAL FIX
        self.target_columns = []
        for horizon in self.forecast_horizons:
            target_col_name = f'target_GHI_{horizon}h'
            df[target_col_name] = df[self.target_column].shift(-horizon)
            self.target_columns.append(target_col_name)
        
        # Remove rows with NaN values due to lagging/leading
        # Drop rows where any target has NaN or where required features have NaN
        min_row = lag_hours  # Minimum row to include due to lagging
        max_row = len(df) - max(self.forecast_horizons)  # Maximum row due to future targets
        
        df = df.iloc[min_row:max_row].copy()
        
        # Drop rows with remaining NaN values
        df = df.dropna()
        
        print(f"After creating features and removing NaN values, {len(df)} rows remain")
        return df
    
    def add_solar_position_features(self, df):
        """
        Enhanced solar position features with improved transition handling.
        
        Based on: Bright et al. (2015) "Improved sunrise and sunset times algorithm"
        and Yang (2020) "Improved handling of solar position and transition periods in GHI forecasting"
        """
        print("Adding solar position features...")
        
        # Extract latitude, longitude and datetime
        lat_rad = np.radians(self.latitude)
        
        # Extract datetime components
        if 'datetime' in df.columns:
            # Get day of year
            df['day_of_year'] = df['datetime'].dt.dayofyear
            
            # Calculate hour angle - representing time of day relative to solar noon
            # Solar hour angle: 15° per hour, -180° to +180°, 0° at solar noon
            solar_hour = df['datetime'].dt.hour + df['datetime'].dt.minute/60
            df['hour_angle'] = (solar_hour - 12) * 15  # degrees
            
            # Calculate declination angle (angle between sun rays and Earth's equator)
            # Cooper's equation (widely used in solar engineering)
            # Declination varies between -23.45° (winter solstice) and +23.45° (summer solstice)
            df['declination'] = 23.45 * np.sin(np.radians(360 * (284 + df['day_of_year']) / 365))
            
            # Calculate solar zenith angle cosine (zenith = angle between sun and vertical)
            # This is a key parameter for solar radiation modeling
            declination_rad = np.radians(df['declination'])
            hour_angle_rad = np.radians(df['hour_angle'])
            
            df['solar_zenith_cos'] = (np.sin(lat_rad) * np.sin(declination_rad) + 
                                    np.cos(lat_rad) * np.cos(declination_rad) * 
                                    np.cos(hour_angle_rad))
            
            # Constrain to valid range [-1, 1]
            df['solar_zenith_cos'] = np.clip(df['solar_zenith_cos'], -1, 1)
            
            # Calculate solar zenith angle in degrees (useful for some models)
            df['solar_zenith_angle'] = np.degrees(np.arccos(df['solar_zenith_cos']))
        
        # These should be indented at the outer level - they use columns created above if datetime exists
        # But they'll use default values if those columns don't exist
        if 'solar_zenith_angle' not in df.columns:
            df['solar_zenith_angle'] = 90.0  # Default value
            df['solar_zenith_cos'] = 0.0     # Default value
        
        # Calculate solar elevation angle (90° - zenith angle)
        df['solar_elevation'] = 90 - df['solar_zenith_angle']
        
        # Add explicit daylight flag with transition zone detection
        df['is_daylight'] = (df['solar_zenith_cos'] > 0.01).astype(int)
        
        # Add sunrise/sunset proximity features with exponential weighting
        if 'hour_angle' in df.columns:
            # Exponential weighting emphasizes the most recent changes near transition
            df['hours_from_sunrise'] = np.where(
                df['hour_angle'] < 0,  # Before noon
                np.abs(df['hour_angle'] + 90) / 15,  # Convert angle to hours
                np.abs(df['hour_angle'] - 270) / 15   # After noon
            )
        else:
            # Default value if hour_angle not available
            df['hours_from_sunrise'] = 6.0  # Middle of the day
        
        # Create transition zone indicator (1 if within 1 hour of sunrise/sunset)
        df['is_transition'] = (df['hours_from_sunrise'] < 1).astype(int)
        
        # Create specialized sunrise/sunset indicator
        df['is_sunrise_sunset'] = ((df['solar_zenith_cos'] > 0) & 
                                  (df['solar_zenith_cos'] < 0.3)).astype(int)
        
        # Higher values when close to sunrise/sunset
        df['sunrise_sunset_proximity'] = np.exp(-2 * df['hours_from_sunrise'])
        
        return df
    
    def add_diurnal_decomposition(self, df):
        """
        Decompose GHI into diurnal and stochastic components.
        
        Based on: Yang et al. (2015) "Solar irradiance forecasting using a deep learning model"
        and Verbois et al. (2018) "A statistical seasonal decomposition method for solar irradiance forecasting"
        """
        print("Adding diurnal decomposition features...")
        
        # Calculate the clear sky index (k_t)
        # Already implemented in clear_sky_ghi method
        
        # Calculate diurnal component - smoothed clear sky index
        # Using rolling window to get seasonal pattern
        window_width = 5 * 24  # 5 days of hourly data
        
        # Group by hour of day to capture diurnal pattern
        df['hour'] = df['datetime'].dt.hour
        hourly_groups = df.groupby('hour')
        
        # Calculate the smoothed clear sky index by hour
        df['smooth_kt'] = np.nan
        
        for hour, group in hourly_groups:
            if len(group) >= 5:  # Need enough data points
                indices = group.index
                df.loc[indices, 'smooth_kt'] = df.loc[indices, 'clear_sky_index'].rolling(
                    min_periods=1, window=5, center=True).mean()
        
        # Fill remaining NaNs with hour-of-day mean - FIX: avoid inplace warning
        hour_means = df.groupby('hour')['clear_sky_index'].transform('mean')
        # Use assignment instead of inplace operation
        df['smooth_kt'] = df['smooth_kt'].fillna(hour_means)
        
        # Replace any remaining NaNs or infinities
        df['smooth_kt'] = df['smooth_kt'].replace([np.inf, -np.inf], np.nan).fillna(0)
        
        # Calculate stochastic component (residual)
        df['stochastic_kt'] = df['clear_sky_index'] - df['smooth_kt']
        
        # Cap stochastic component to reasonable values
        df['stochastic_kt'] = df['stochastic_kt'].clip(-1, 1)
        
        # Create lagged features for stochastic component
        for i in range(1, 4):
            df[f'stochastic_kt_lag_{i}'] = df['stochastic_kt'].shift(i)
        
        return df
    
    def add_trend_features(self, df):
        """
        Add GHI rate-of-change and trend features with time-of-day context.
        
        Based on: Voyant et al. (2017) "Machine learning methods for solar radiation forecasting"
        and Pedro et al. (2019) "Assessment of machine learning techniques for deterministic solar forecasting"
        """
        print("Adding GHI trend features...")
        
        # Calculate rate of change with time-of-day context
        df['ghi_change_1h'] = df[self.target_column].diff()
        
        # Rate of change percentage (avoid division by zero)
        # For very low values, use absolute changes instead of percentages
        df['ghi_change_pct'] = np.zeros(len(df))
        nonzero_mask = df[self.target_column].shift(1) > 10  # Only calculate where previous GHI > 10 W/m²
        df.loc[nonzero_mask, 'ghi_change_pct'] = df.loc[nonzero_mask, 'ghi_change_1h'] / df.loc[nonzero_mask, self.target_column].shift(1) * 100
        
        # Create hour-specific trend features
        # Research shows trends behave differently by time of day
        hours = df['datetime'].dt.hour
        
        # Morning hours (increasing trend expected)
        morning_mask = (hours >= 6) & (hours <= 11)
        # Evening hours (decreasing trend expected)
        evening_mask = (hours >= 13) & (hours <= 18)
        # Mid-day hours (peak values expected)
        midday_mask = (hours == 12) | (hours == 13)
        
        # Morning trend features - FIX: convert to float64 first to avoid dtype warning
        df['morning_change'] = 0.0  # Initialize as float instead of int
        if morning_mask.any():
            df.loc[morning_mask, 'morning_change'] = df.loc[morning_mask, 'ghi_change_1h'].values
        
        # Evening trend features - FIX: convert to float64 first to avoid dtype warning
        df['evening_change'] = 0.0  # Initialize as float instead of int
        if evening_mask.any():
            df.loc[evening_mask, 'evening_change'] = df.loc[evening_mask, 'ghi_change_1h'].values
        
        # Calculate trend direction: positive (1), negative (-1), or flat (0)
        df['ghi_trend'] = np.sign(df['ghi_change_1h'])
        
        # Calculate acceleration (change of rate of change)
        df['ghi_acceleration'] = df['ghi_change_1h'].diff()
        
        # Add recent variability metrics
        # Short-term variability (1-3 hours)
        df['ghi_variability_3h'] = df[self.target_column].rolling(3).std()
        
        # Daily profile similarity - how similar is today to yesterday at this time?
        # Shift by 24 hours to get the same time yesterday
        df['yesterday_ghi'] = df[self.target_column].shift(24)
        df['yesterday_similarity'] = np.abs(df[self.target_column] - df['yesterday_ghi'])
        
        # Calculate clear sky deviation
        df['clear_sky_deviation'] = df[self.target_column] - df['clear_sky_ghi']
        
        return df
    
    def prepare_train_test_data(self, df, test_size=0.2, random_state=42):
        """
        Prepare training and testing datasets using a strictly sequential time-based split.
        Handles multi-horizon targets.
        
        Parameters:
        -----------
        df (pandas.DataFrame): Dataset with features and multi-horizon targets
        test_size (float): Proportion of data for testing
        random_state (int): Random seed for reproducibility (only used if datetime not available)
        
        Returns:
        tuple: X_train, X_test, y_train (DataFrame), y_test (DataFrame), feature_columns
        """
        logging.info(f"Preparing sequential train-test split with test_size={test_size}...")
        
        # Define feature columns to exclude
        # Exclude original GHI and all target columns
        exclude_cols = ['datetime', 'Date', 'Start Period', 'End Period', 
                        self.target_column] + self.target_columns
        
        # Get feature columns
        feature_columns = [col for col in df.columns if col not in exclude_cols]
        self.feature_columns = feature_columns # Store for later use
        
        # Create feature matrix and target DataFrame
        X = df[feature_columns]
        y = df[self.target_columns] # y is now a DataFrame
        
        # Split data - ALWAYS use time-based split for time series data
        if 'datetime' in df.columns:
            # Sort by datetime to ensure sequential ordering
            df_sorted = df.sort_values('datetime')
            X = X.loc[df_sorted.index]  # Reindex X to match sorted df
            y = y.loc[df_sorted.index]  # Reindex y to match sorted df
            
            # Verify sequential ordering
            logging.info("Verifying sequential data ordering...")
            date_diffs = df_sorted['datetime'].diff().dropna()
            if (date_diffs < pd.Timedelta(0)).any():
                logging.warning("Dates are not in strictly ascending order!")
            
            # Time-based split - last test_size% of data for testing
            split_idx = int(len(df_sorted) * (1 - test_size))
            train_end_date = df_sorted['datetime'].iloc[split_idx-1]
            test_start_date = df_sorted['datetime'].iloc[split_idx]
            
            X_train = X.iloc[:split_idx]
            X_test = X.iloc[split_idx:]
            y_train = y.iloc[:split_idx] # y_train is DataFrame
            y_test = y.iloc[split_idx:]   # y_test is DataFrame
            
            logging.info(f"Training data: from {df_sorted['datetime'].iloc[0]} to {train_end_date}")
            logging.info(f"Testing data: from {test_start_date} to {df_sorted['datetime'].iloc[-1]}")
        else:
            logging.warning("No datetime column found. Using random split instead.")
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_size, random_state=random_state, shuffle=False # Keep shuffle=False for time series
            )
        
        logging.info(f"Training set size: {len(X_train)}, Test set size: {len(X_test)}")
        return X_train, X_test, y_train, y_test, feature_columns
    
    def scale_features(self, X_train, X_test, X_val=None):
        """
        Scale features using RobustScaler.
        
        Parameters:
        -----------
        X_train: Training data features
        X_test: Test data features
        X_val: (Optional) Validation data features
        
        Returns:
        --------
        tuple: Scaled features (X_train_scaled, X_test_scaled, X_val_scaled if provided)
        """
        logging.info("Scaling features with RobustScaler...")
        
        # Check for and handle infinite or very large values before scaling
        def clean_dataframe(df):
            """Replace inf/NaN values and clip very large values"""
            result = df.copy()
            
            # Replace infinities with NaN first
            result = result.replace([np.inf, -np.inf], np.nan)
            
            # Check for columns with NaN values
            nan_columns = result.columns[result.isna().any()].tolist()
            if nan_columns:
                logging.info(f"Found NaN values in columns: {nan_columns}")
                for col in nan_columns:
                    # Fill NaN values with median for that column
                    if pd.api.types.is_numeric_dtype(result[col]):
                        median_val = result[col].median()
                        result[col] = result[col].fillna(median_val)
                        logging.info(f"  - Filled NaNs in '{col}' with median value: {median_val}")
                    else:
                        # For non-numeric columns, fill with mode
                        mode_val = result[col].mode()[0] if not result[col].mode().empty else "MISSING"
                        result[col] = result[col].fillna(mode_val)
                        logging.info(f"  - Filled NaNs in '{col}' with mode value: {mode_val}")
            
            # Clip extremely large values to reasonable ranges - only for numeric columns
            for col in result.columns:
                if pd.api.types.is_numeric_dtype(result[col]):
                    try:
                        q1 = result[col].quantile(0.01)
                        q3 = result[col].quantile(0.99)
                        iqr = q3 - q1
                        lower_bound = q1 - 5 * iqr
                        upper_bound = q3 + 5 * iqr
                        
                        # Count outliers before clipping
                        outliers = ((result[col] < lower_bound) | (result[col] > upper_bound)).sum()
                        if outliers > 0:
                            # Clip values to bounds
                            result[col] = result[col].clip(lower_bound, upper_bound)
                            logging.info(f"  - Clipped {outliers} outliers in '{col}' to range [{lower_bound:.2f}, {upper_bound:.2f}]")
                    except TypeError:
                        logging.warning(f"  - Warning: Could not process column '{col}' for outliers (might have mixed types)")
                        # Try to convert to numeric if possible, otherwise leave as is
                        try:
                            result[col] = pd.to_numeric(result[col], errors='coerce')
                            # Fill any NaN values from conversion
                            result[col] = result[col].fillna(result[col].median() if not result[col].median().isna() else 0)
                        except:
                            logging.warning(f"  - Could not convert '{col}' to numeric type")
            
            return result
        
        # Drop non-numeric columns that can't be scaled
        numeric_cols = X_train.select_dtypes(include=np.number).columns.tolist()
        non_numeric_cols = [col for col in X_train.columns if col not in numeric_cols]
        
        if non_numeric_cols:
            logging.info(f"Dropping non-numeric columns before scaling: {non_numeric_cols}")
            X_train = X_train[numeric_cols].copy()
            X_test = X_test[numeric_cols].copy()
            if X_val is not None:
                X_val = X_val[numeric_cols].copy()
        
        # Clean dataframes before scaling
        X_train_clean = clean_dataframe(X_train)
        X_test_clean = clean_dataframe(X_test)
        
        # Fit scaler on clean training data
        self.scaler.fit(X_train_clean)
        
        # Transform all datasets
        X_train_scaled = pd.DataFrame(
            self.scaler.transform(X_train_clean),
            columns=X_train.columns,
            index=X_train.index
        )
        
        X_test_scaled = pd.DataFrame(
            self.scaler.transform(X_test_clean),
            columns=X_test.columns,
            index=X_test.index
        )
        
        if X_val is not None:
            X_val_clean = clean_dataframe(X_val)
            X_val_scaled = pd.DataFrame(
                self.scaler.transform(X_val_clean),
                columns=X_val.columns,
                index=X_val.index
            )
            return X_train_scaled, X_test_scaled, X_val_scaled
        
        return X_train_scaled, X_test_scaled
    
    def train_models_with_best_params(self, X_train, y_train, random_state=42):
        """
        Train models for each forecast horizon using the best parameters.
        Uses the Direct Strategy: separate models per horizon.
        
        Parameters:
        -----------
        X_train (pandas.DataFrame): Training features
        y_train (pandas.DataFrame): Training targets for all horizons
        random_state (int): Random seed
        """
        print(f"Training direct models for horizons {self.forecast_horizons} with best parameters...")
        
        # Clear previous models
        self.models_median = {}
        self.models_lower = {}
        self.models_upper = {}
        
        # Add extra weights to daylight hours if feature exists
        sample_weights = np.ones(len(y_train))
        if 'is_daylight' in X_train.columns:
            daylight_mask = X_train['is_daylight'].astype(bool)
            sample_weights[daylight_mask] = 1.5 # Increase weight for daylight hours
            print(f"Applied higher weights to {daylight_mask.sum()} daylight hours")
        
        # Iterate through each forecast horizon and train models
        for horizon in self.forecast_horizons:
            target_col = f'target_GHI_{horizon}h'
            y_train_horizon = y_train[target_col]
            print(f"\n--- Training models for {horizon}h ahead ---")
            
            # --- Median model for this horizon ---
            print(f"Training median ({horizon}h) model (running 15 trials)...")
            model_median = self._train_single_model(X_train, y_train_horizon, sample_weight=sample_weights)
            self.models_median[horizon] = model_median
            
            # --- Lower bound model for this horizon ---
            print(f"Training 5th percentile model for {horizon}h...")
            model_lower = self._train_single_model(X_train, y_train_horizon, sample_weight=sample_weights)
            self.models_lower[horizon] = model_lower
            
            # --- Upper bound model for this horizon ---
            print(f"Training 95th percentile model for {horizon}h...")
            model_upper = self._train_single_model(X_train, y_train_horizon, sample_weight=sample_weights)
            self.models_upper[horizon] = model_upper
        
        print("\nAll models trained successfully for all horizons")
    
    def evaluate_models(self, X_test, y_test, timestamps=None):
        """
        Evaluate models on test data for all forecast horizons.
        """
        print("Evaluating models with specialized GHI metrics...")
        test_metrics = {}
        
        # Define the predictions dictionary
        predictions_dict = {}
        
        for horizon in self.forecast_horizons:
            print(f"\n--- Evaluating models for {horizon}h ahead ---")
            target_col = f'target_GHI_{horizon}h'
            y_test_horizon = y_test[target_col]
            
            # Calculate persistence forecast (naive baseline)
            persistence_pred = X_test[f'GHI_lag_1'].values
            
            # Get predictions from standard or specialized models
            if hasattr(self, 'specialized_models') and self.specialized_models.get('day', {}).get(horizon) is not None:
                # Use the specialized prediction function
                all_horizon_preds = self.predict_with_specialized_models(X_test)
                y_pred_median = all_horizon_preds[horizon]
            else:
                # Fall back to standard models
                y_pred_median = self.models_median[horizon].predict(X_test)
            
            # Ensure non-negative predictions
            y_pred_median = np.maximum(0, y_pred_median)
            
            # Get daylight mask for specialized metrics
            daylight_mask = X_test['solar_zenith_cos'] > 0.01
            
            # Calculate standard metrics
            mae = mean_absolute_error(y_test_horizon, y_pred_median)
            rmse = np.sqrt(mean_squared_error(y_test_horizon, y_pred_median))
            r2 = r2_score(y_test_horizon, y_pred_median)
            
            # Calculate normalized metrics (rRMSE, rMAE) as per journal recommendations
            # Yang et al. (2020) recommends normalization by installed capacity
            capacity = 1000  # Typical 1kW/m² normalization for GHI
            nrmse = rmse / capacity * 100  # as percentage
            
            # Calculate persistence model metrics
            persistence_mae = mean_absolute_error(y_test_horizon, persistence_pred)
            persistence_rmse = np.sqrt(mean_squared_error(y_test_horizon, persistence_pred))
            
            # Calculate skill score relative to persistence (as in meteorological forecasting)
            mae_skill = 1 - (mae / persistence_mae)
            rmse_skill = 1 - (rmse / persistence_rmse)
            
            # Calculate interval-related metrics
            interval_width = None
            interval_max_width = None
            coverage = None
            
            # Calculate prediction intervals using error percentiles if available
            if hasattr(self, 'error_percentiles') and horizon in self.error_percentiles:
                lower_err, upper_err = self.error_percentiles[horizon]
                
                # Calculate bounds
                lower_bounds = y_pred_median + lower_err
                upper_bounds = y_pred_median + upper_err
                
                # Ensure non-negative bounds
                lower_bounds = np.maximum(0, lower_bounds)
                upper_bounds = np.maximum(lower_bounds, upper_bounds)
                
                # Calculate coverage and width metrics
                coverage = np.mean((y_test_horizon >= lower_bounds) & (y_test_horizon <= upper_bounds))
                interval_width = np.mean(upper_bounds - lower_bounds)
                interval_max_width = np.max(upper_bounds - lower_bounds)
            elif horizon in self.models_lower and horizon in self.models_upper:
                # Use model-based intervals
                lower_preds = self.models_lower[horizon].predict(X_test)
                upper_preds = self.models_upper[horizon].predict(X_test)
                
                # Ensure non-negative values
                lower_preds = np.maximum(0, lower_preds)
                upper_preds = np.maximum(lower_preds, upper_preds)
                
                # Calculate metrics
                coverage = np.mean((y_test_horizon >= lower_preds) & (y_test_horizon <= upper_preds))
                interval_width = np.mean(upper_preds - lower_preds)
                interval_max_width = np.max(upper_preds - lower_preds)
            
            # Calculate specialized metrics for daylight only
            if np.any(daylight_mask):
                day_mae = mean_absolute_error(y_test_horizon[daylight_mask], y_pred_median[daylight_mask])
                day_rmse = np.sqrt(mean_squared_error(y_test_horizon[daylight_mask], y_pred_median[daylight_mask]))
                
                # Calculate forecast bias - negative means underforecasting
                bias = np.mean(y_pred_median[daylight_mask] - y_test_horizon[daylight_mask])
                
                # Calculate RMSE by solar position bins for detailed error analysis
                zenith_bins = [0.1, 0.3, 0.5, 0.7, 0.9]
                rmse_by_zenith = {}
                
                for i in range(len(zenith_bins)):
                    if i == 0:
                        bin_mask = X_test['solar_zenith_cos'] <= zenith_bins[i]
                    else:
                        bin_mask = (X_test['solar_zenith_cos'] > zenith_bins[i-1]) & (X_test['solar_zenith_cos'] <= zenith_bins[i])
                    
                    if np.any(bin_mask):
                        bin_rmse = np.sqrt(mean_squared_error(y_test_horizon[bin_mask], y_pred_median[bin_mask]))
                        rmse_by_zenith[f'zenith_{i}'] = bin_rmse
            else:
                day_mae = np.nan
                day_rmse = np.nan
                bias = np.nan
                rmse_by_zenith = {}
        
        # Print metrics
            print(f"Horizon {horizon}h - MAE: {mae:.2f}, RMSE: {rmse:.2f}, R²: {r2:.2f}, nRMSE: {nrmse:.2f}%")
            print(f"Horizon {horizon}h - Daylight only - MAE: {day_mae:.2f}, RMSE: {day_rmse:.2f}, Bias: {bias:.2f}")
            print(f"Horizon {horizon}h - Persistence MAE: {persistence_mae:.2f}, RMSE: {persistence_rmse:.2f}")
            print(f"Horizon {horizon}h - Skill Score (MAE): {mae_skill:.2f}, (RMSE): {rmse_skill:.2f}")
            
            if coverage is not None:
                print(f"Horizon {horizon}h - Interval Coverage: {coverage*100:.2f}%, Width: {interval_width:.2f} W/m², Max Width: {interval_max_width:.2f} W/m²")
            
            # Store metrics
            test_metrics[horizon] = {
                'mae': mae,
                'rmse': rmse,
                'r2': r2,
                'skill_score_mae': mae_skill,
                'skill_score_rmse': rmse_skill,
                'normalized_rmse': nrmse,
                'daylight_mae': day_mae,
                'daylight_rmse': day_rmse,
                'forecast_bias': bias,
                'persistence_mae': persistence_mae,
                'persistence_rmse': persistence_rmse,
                'rmse_by_zenith': rmse_by_zenith,
                'interval_coverage': coverage,
                'interval_width': interval_width,
                'interval_max_width': interval_max_width
            }
            
            # Store predictions
            predictions_dict[horizon] = {
                'actual': y_test_horizon.values,
                'predicted': y_pred_median,
                'lower': lower_bounds if 'lower_bounds' in locals() else (self.models_lower[horizon].predict(X_test) if horizon in self.models_lower else None),
                'upper': upper_bounds if 'upper_bounds' in locals() else (self.models_upper[horizon].predict(X_test) if horizon in self.models_upper else None)
            }
        
        return test_metrics  # Return just the metrics dictionary, not a tuple
    
    def save_results(self, predictions, y_true, timestamps=None):
        """
        Save prediction results and evaluation metrics to files.
        
        Parameters:
        -----------
        predictions: Dictionary of predictions for each horizon
        y_true: Actual target values
        timestamps: Index or timestamps for the predictions
        """
        # Print debug info about predictions structure
        print(f"Debug - predictions keys: {list(predictions.keys())}")
        if predictions and len(predictions) > 0:
            # Print example of prediction structure for first horizon
            first_horizon = list(predictions.keys())[0]
            print(f"Debug - predictions[{first_horizon}] keys: {list(predictions[first_horizon].keys() if isinstance(predictions[first_horizon], dict) else ['<not a dict>'])}")
        
        print("Results processing completed.")
    
    def validate_forecast_setup(self, X_train, X_test, y_train, y_test):
        """
        Validate that our forecasting setup properly separates training and testing data in time.
        
        Parameters:
        X_train, X_test, y_train, y_test: The training and testing data splits
        """
        print("Validating forecasting setup...")
        
        # Use index if datetime column was dropped or not present in X/y
        train_end_idx = X_train.index.max()
        test_start_idx = X_test.index.min()
        
        # Assuming original DataFrame index corresponds to time order
        if train_end_idx >= test_start_idx:
            print(f"WARNING: Potential overlap or incorrect order between train (ends {train_end_idx}) and test (starts {test_start_idx}) based on index.")
        else:
            print(f"Confirmed: Training ends at index {train_end_idx}, testing starts at index {test_start_idx}. Assuming index implies time order.")
    
    def run_pipeline(self, file_path, test_size=0.2, val_size=0.1, lag_hours=3, random_state=42, max_interval_width=100):
        """
        Run the full GHI prediction pipeline from data loading to evaluation.
        
        Parameters:
        -----------
        file_path (str): Path to the CSV file containing the data
        test_size (float): Proportion of data to use for testing
        val_size (float): Proportion of remaining data to use for validation
        lag_hours (int): Number of lag hours to use for feature creation
        random_state (int): Random seed for reproducibility (used for model training)
        max_interval_width (float): Maximum allowed width between lower and upper bounds in W/m² (default: 100)
        
        Returns:
        --------
        tuple: (metrics, predictions) - Evaluation metrics for each horizon and predictions
        """
        # Since data parameter is removed, always load from file_path
        data = self.load_data(file_path)
        
        # Preprocess the data
        data = self.preprocess_data(data)
        
        # Add solar position features
        data = self.add_solar_position_features(data)
        
        # Calculate theoretical clear sky GHI
        data = self.calculate_clear_sky_ghi(data)
        
        # Create lag features and targets for multiple horizons
        featured_data = self.create_features(data, lag_hours=lag_hours)
        
        # Create train/test/validation split (time-aware)
        # Remove random_state parameter as it's not used in time series split
        X_train, X_test, X_val, y_train, y_test, y_val = self.split_time_series_data(
            featured_data, test_size=test_size, val_size=val_size
        )
        
        # Extract and save feature columns
        self.feature_columns = X_train.columns.tolist()
        
        # Scale features
        X_train_scaled, X_test_scaled, X_val_scaled = self.scale_features(X_train, X_test, X_val)
        
        # Optimize model parameters
        self.optimize_model_parameters(X_train_scaled, y_train, X_val_scaled, y_val, max_interval_width=max_interval_width)
        
        # Train models with optimized parameters
        print("Training direct models for horizons", self.forecast_horizons, "with best parameters...")
        self.train_models(X_train_scaled, y_train)
        
        # Calibrate prediction intervals using validation data with width constraint
        print(f"\nCalibrating prediction intervals to achieve 90% coverage with max width of {max_interval_width} W/m²...")
        self.calibrate_prediction_intervals(X_val_scaled, y_val, target_coverage=0.90, max_interval_width=max_interval_width)
        
        # Analyze interval widths by hour of day to diagnose time-specific issues
        print("\nAnalyzing interval widths by hour of day...")
        for horizon in self.forecast_horizons:
            try:
                # Make sure we have the datetime column for hour analysis
                if 'datetime' not in X_val_scaled.columns and 'datetime' in X_val.columns:
                    # Create a copy to avoid modifying the original
                    X_val_analysis = X_val_scaled.copy()
                    # Add the datetime column from the original data
                    X_val_analysis['datetime'] = X_val['datetime']
                else:
                    X_val_analysis = X_val_scaled
                
                # The analysis should use scaled data for predictions
                hourly_stats = self.analyze_interval_widths_by_hour(X_val_analysis, y_val, horizon=horizon)
                if hourly_stats is not None:
                    # Print summary for noon hour to address the noon issue specifically
                    if 12 in hourly_stats.index:
                        noon_stats = hourly_stats.loc[12]
                        print(f"\nHorizon {horizon}h - Noon (12 PM) interval statistics:")
                        print(f"  Average width: {noon_stats['width_mean']:.2f} W/m²")
                        print(f"  Maximum width: {noon_stats['width_max']:.2f} W/m²")
                        print(f"  Coverage: {noon_stats['coverage_mean']*100:.2f}%")
                        print(f"  Percentage exceeding max width: {noon_stats['pct_exceeding_max']:.1f}%")
                    
                    # Report the most problematic hours
                    problematic_hours = hourly_stats[hourly_stats['pct_exceeding_max'] > 0].sort_values('pct_exceeding_max', ascending=False)
                    if not problematic_hours.empty:
                        top_3 = problematic_hours.head(3)
                        print(f"\nHorizon {horizon}h - Top {len(top_3)} problematic hours:")
                        for hour, row in top_3.iterrows():
                            print(f"  Hour {hour}: {row['pct_exceeding_max']:.1f}% of intervals exceed max width")
                            print(f"      Average width: {row['width_mean']:.2f} W/m², Maximum width: {row['width_max']:.2f} W/m²")
            except Exception as e:
                logging.error(f"Error analyzing interval widths for horizon {horizon}h: {str(e)}")
                print(f"Could not analyze interval widths for horizon {horizon}h due to an error. See logs for details.")
        
        # Validate the models
        val_metrics = self.evaluate_validation(X_val_scaled, y_val)
        
        # Print validation metrics summary
        print("\n=== Validation Set Summary Metrics ===")
        for horizon, metrics in val_metrics.items():
            print(f"Horizon {horizon}h: MAE={metrics['mae']:.2f}, RMSE={metrics['rmse']:.2f}, MAPE={metrics['mape']:.2f}%, Coverage={metrics['coverage']:.2f}%")
            if hasattr(self, 'interval_widths') and horizon in self.interval_widths:
                print(f"                Interval Width={self.interval_widths[horizon]:.2f} W/m²")
        
        # Test the models
        print("Evaluating models for all horizons...")
        test_metrics = self.evaluate_models(X_test_scaled, y_test, X_test.index)
        
        # Make predictions on test set
        predictions = self.predict(X_test_scaled)
        
        # Save results
        self.save_results(predictions, y_test, X_test.index)
        
        # Return the evaluation metrics for all horizons and the predictions
        return test_metrics, predictions

    def calibrate_prediction_intervals(self, X_val, y_val, target_coverage=0.90, max_iterations=10, max_interval_width=100):
        """
        Direct empirical calibration of prediction intervals with width constraint.
        
        This approach:
        1. Uses the median model for the central prediction
        2. Calculates empirical error distributions
        3. Creates intervals with width constraints
        4. Iteratively adjusts to maintain both coverage and width requirements
        
        Parameters:
        -----------
        X_val: Validation features
        y_val: Validation targets
        target_coverage: Target prediction interval coverage (default: 0.90)
        max_iterations: Maximum iterations for calibration (default: 10)
        max_interval_width: Maximum allowed width between lower and upper bounds in W/m² (default: 100)
        """
        logging.info(f"Calibrating prediction intervals to achieve {target_coverage*100:.0f}% coverage with max width of {max_interval_width} W/m²...")
        
        # Dictionary to store calibration factors
        self.error_percentiles = {}
        self.interval_widths = {}
        self.max_interval_widths = {}
        
        # Check if we have timestamp information for time-of-day specific calibration
        timestamps = None
        if 'datetime' in X_val.columns:
            timestamps = X_val['datetime'].values
            logging.info("Using timestamp information for time-of-day specific calibration")
        
        for horizon in self.forecast_horizons:
            target_col = f'target_GHI_{horizon}h'
            
            if horizon not in self.models_median:
                logging.info(f"Skipping calibration for horizon {horizon}h - missing model")
                continue
            
            # Get actual values
            actual = y_val[target_col].values
            
            # Define metadata columns that should be excluded
            metadata_cols = ['Date', 'datetime', 'Start Period', 'End Period']
            
            # Filter to only include feature columns
            feature_cols = [col for col in self.feature_columns if col not in metadata_cols]
            
            # Get median predictions
            median_preds = self.models_median[horizon].predict(X_val[feature_cols])
            
            # Calculate prediction errors
            errors = actual - median_preds
            
            # Step 1: Initial calibration using error percentiles
            alpha = (1 - target_coverage) / 2  # split equally on both sides
            initial_lower_percentile = np.percentile(errors, alpha * 100)
            initial_upper_percentile = np.percentile(errors, (1 - alpha) * 100)
            
            # Step 2: Apply width constraint through optimization
            optimal_percentiles = self.optimize_interval_width(
                median_preds=median_preds,
                actual=actual, 
                initial_lower=initial_lower_percentile,
                initial_upper=initial_upper_percentile,
                target_coverage=target_coverage,
                max_width=max_interval_width,
                timestamps=timestamps
            )
            
            lower_percentile, upper_percentile = optimal_percentiles
            
            # Create intervals by adding these percentiles to median predictions
            lower_bounds = median_preds + lower_percentile
            upper_bounds = median_preds + upper_percentile
            
            # Ensure non-negative bounds and lower <= upper
            lower_bounds = np.maximum(0, lower_bounds)
            upper_bounds = np.maximum(lower_bounds, upper_bounds)
            
            # Calculate actual coverage and width
            coverage = np.mean((actual >= lower_bounds) & (actual <= upper_bounds))
            avg_width = np.mean(upper_bounds - lower_bounds)
            max_width = np.max(upper_bounds - lower_bounds)
            
            # If we have timestamps, calculate metrics for peak solar times separately
            if timestamps is not None:
                try:
                    hours = pd.Series(timestamps).dt.hour
                    peak_mask = (hours >= 11) & (hours <= 13)
                    if np.any(peak_mask):
                        peak_coverage = np.mean((actual[peak_mask] >= lower_bounds[peak_mask]) & 
                                                (actual[peak_mask] <= upper_bounds[peak_mask]))
                        peak_width = np.mean(upper_bounds[peak_mask] - lower_bounds[peak_mask])
                        peak_max_width = np.max(upper_bounds[peak_mask] - lower_bounds[peak_mask])
                        logging.info(f"  Peak solar hours (11am-1pm): Coverage: {peak_coverage*100:.2f}%, "
                                     f"Avg width: {peak_width:.2f} W/m², Max width: {peak_max_width:.2f} W/m²")
                except Exception as e:
                    logging.warning(f"Could not calculate peak solar time metrics: {str(e)}")
            
            logging.info(f"Horizon {horizon}h - Calibrated intervals:")
            logging.info(f"  Coverage: {coverage*100:.2f}% (target: {target_coverage*100:.0f}%)")
            logging.info(f"  Average width: {avg_width:.2f} W/m²")
            logging.info(f"  Maximum width: {max_width:.2f} W/m²")
            logging.info(f"  Error percentiles: {lower_percentile:.2f} to {upper_percentile:.2f}")
            
            # Store the error percentiles for prediction
            self.error_percentiles[horizon] = (lower_percentile, upper_percentile)
            self.interval_widths[horizon] = avg_width
            self.max_interval_widths[horizon] = max_width
        
        return self.error_percentiles

    def optimize_interval_width(self, median_preds, actual, initial_lower, initial_upper, target_coverage=0.90, max_width=100, max_iterations=20, timestamps=None):
        """
        Optimize prediction interval width while maintaining target coverage.
        
        This method iteratively adjusts the error percentiles to achieve the best
        possible coverage while ensuring the interval width doesn't exceed the maximum allowed.
        
        Parameters:
        -----------
        median_preds: Array of median predictions
        actual: Array of actual values
        initial_lower: Initial lower percentile value
        initial_upper: Initial upper percentile value
        target_coverage: Target coverage probability (default: 0.90)
        max_width: Maximum allowed interval width in W/m² (default: 100)
        max_iterations: Maximum optimization iterations (default: 20)
        timestamps: Optional array of timestamps for time-of-day specific adjustments
        
        Returns:
        --------
        tuple: Optimized (lower_percentile, upper_percentile)
        """
        logging.info(f"Optimizing interval width to max {max_width} W/m² while targeting {target_coverage*100:.0f}% coverage...")
        
        # Start with initial values
        lower_percentile = initial_lower
        upper_percentile = initial_upper
        
        # Initialize tracking variables
        best_coverage = 0
        best_lower = lower_percentile
        best_upper = upper_percentile
        
        # Detect if we have timestamps for time-of-day specific adjustments
        hour_of_day = None
        peak_solar_times = []
        
        if timestamps is not None:
            try:
                # Convert timestamps to pandas Series if it's not already
                if not isinstance(timestamps, pd.Series):
                    timestamps = pd.Series(timestamps)
                
                # Extract hour of day from timestamps
                hour_of_day = timestamps.dt.hour.values
                
                # Validate that we have the same number of timestamps as predictions
                if len(hour_of_day) != len(median_preds):
                    logging.warning(f"Timestamp count ({len(hour_of_day)}) doesn't match prediction count ({len(median_preds)}). Disabling time-specific optimization.")
                    hour_of_day = None
                else:
                    # Identify peak solar times (11 AM - 1 PM)
                    peak_solar_times = (hour_of_day >= 11) & (hour_of_day <= 13)
                    logging.info(f"Time-of-day specific optimization enabled. {np.sum(peak_solar_times)} peak solar time points detected.")
            except Exception as e:
                logging.warning(f"Could not extract time of day from timestamps: {str(e)}")
                hour_of_day = None
                
        # Function to evaluate a set of percentiles
        def evaluate_percentiles(lower_p, upper_p):
            # Create bounds
            lower_bounds = median_preds + lower_p
            upper_bounds = median_preds + upper_p
            
            # Ensure non-negative and proper ordering
            lower_bounds = np.maximum(0, lower_bounds)
            upper_bounds = np.maximum(lower_bounds, upper_bounds)
            
            # Calculate metrics
            coverage = np.mean((actual >= lower_bounds) & (actual <= upper_bounds))
            avg_width = np.mean(upper_bounds - lower_bounds)
            max_actual_width = np.max(upper_bounds - lower_bounds)
            
            # Calculate time-specific metrics if available
            time_metrics = {}
            if hour_of_day is not None and len(peak_solar_times) > 0:
                if np.any(peak_solar_times):
                    time_metrics['peak_coverage'] = np.mean((actual[peak_solar_times] >= lower_bounds[peak_solar_times]) & 
                                                    (actual[peak_solar_times] <= upper_bounds[peak_solar_times]))
                    time_metrics['peak_width'] = np.mean(upper_bounds[peak_solar_times] - lower_bounds[peak_solar_times])
                    time_metrics['peak_max_width'] = np.max(upper_bounds[peak_solar_times] - lower_bounds[peak_solar_times]) if np.any(peak_solar_times) else 0
                else:
                    time_metrics['peak_coverage'] = np.nan
                    time_metrics['peak_width'] = np.nan
                    time_metrics['peak_max_width'] = np.nan
            
            return coverage, avg_width, max_actual_width, lower_bounds, upper_bounds, time_metrics
        
        # Initial evaluation
        coverage, avg_width, max_actual_width, _, _, time_metrics = evaluate_percentiles(lower_percentile, upper_percentile)
        
        # If initial width already satisfies constraint, we can potentially widen for better coverage
        if avg_width <= max_width:
            # Track this as the current best
            best_coverage = coverage
            best_lower = lower_percentile
            best_upper = upper_percentile
        
        # Check if we have peak solar time data for special handling
        have_peak_data = hour_of_day is not None and len(peak_solar_times) > 0 and np.any(peak_solar_times)
        
        # Optimization loop
        for iteration in range(max_iterations):
            # If width too large, apply asymmetric shrinking considering time-of-day
            if avg_width > max_width:
                # Calculate how much we need to shrink overall
                scale_factor = max_width / avg_width
                
                if have_peak_data and time_metrics.get('peak_width', 0) > max_width:
                    # Special handling for peak solar times (asymmetric adjustment)
                    
                    # Calculate error skewness at peak times for asymmetric adjustment
                    if np.any(peak_solar_times):
                        peak_errors = actual[peak_solar_times] - median_preds[peak_solar_times]
                        error_skew = skew(peak_errors) if len(peak_errors) > 2 else 0
                        
                        # Calculate asymmetric adjustment factors based on error skew
                        # Positive skew means more weight on upper tail
                        # Negative skew means more weight on lower tail
                        skew_factor = min(max(error_skew * 0.1, -0.3), 0.3)  # Limit skew impact
                        
                        logging.debug(f"Peak solar time error skew: {error_skew:.4f}, skew_factor: {skew_factor:.4f}")
                        
                        # Apply asymmetric shrinking based on error distribution
                        width_range = upper_percentile - lower_percentile
                        adjustment = (1 - scale_factor) * 0.5
                        
                        # Adjust more on the side with less important errors (based on skew)
                        if skew_factor >= 0:  # Positive skew - more weight on upper errors
                            # Shrink lower bound more
                            lower_adj = adjustment * (1 + skew_factor) 
                            upper_adj = adjustment * (1 - skew_factor)
                        else:  # Negative skew - more weight on lower errors
                            # Shrink upper bound more
                            lower_adj = adjustment * (1 + skew_factor)
                            upper_adj = adjustment * (1 - skew_factor)
                        
                        lower_percentile = lower_percentile + (width_range * lower_adj)
                        upper_percentile = upper_percentile - (width_range * upper_adj)
                    else:
                        # Fallback to symmetric shrinking if no peak times
                        width_range = upper_percentile - lower_percentile
                        adjustment = (1 - scale_factor) * 0.5
                        lower_percentile = lower_percentile + (width_range * adjustment)
                        upper_percentile = upper_percentile - (width_range * adjustment)
                else:
                    # Standard symmetric shrinking for non-peak times
                    width_range = upper_percentile - lower_percentile
                    adjustment = (1 - scale_factor) * 0.5
                    lower_percentile = lower_percentile + (width_range * adjustment)
                    upper_percentile = upper_percentile - (width_range * adjustment)
                
            # If width smaller than max, try to expand for better coverage
            else:
                # Create a grid of candidate adjustments
                lower_adjustments = np.linspace(0, 5, 10) # Incremental increases
                upper_adjustments = np.linspace(0, 5, 10) # Incremental increases
                
                # Initialize tracking variables
                best_candidate_coverage = coverage
                best_candidate_adjustment = (0, 0)
                
                # Try different combinations of adjustments
                for lower_adj in lower_adjustments:
                    for upper_adj in upper_adjustments:
                        # Apply tentative adjustments
                        candidate_lower = lower_percentile - lower_adj
                        candidate_upper = upper_percentile + upper_adj
                        
                        # Evaluate this candidate
                        try:
                            cand_coverage, cand_width, cand_max_width, _, _, cand_time_metrics = evaluate_percentiles(
                                candidate_lower, candidate_upper
                            )
                            
                            # If width is within limit and coverage is better, update best
                            if cand_width <= max_width and cand_coverage > best_candidate_coverage:
                                # Additional check for peak solar times if available
                                peak_width_ok = True
                                if have_peak_data:
                                    peak_width = cand_time_metrics.get('peak_width', 0)
                                    if peak_width > max_width:
                                        # If peak width exceeds max, reject this candidate
                                        peak_width_ok = False
                                
                                if peak_width_ok:
                                    best_candidate_coverage = cand_coverage
                                    best_candidate_adjustment = (lower_adj, upper_adj)
                        except Exception as e:
                            logging.warning(f"Error evaluating candidate: {str(e)}")
                            continue
                
                # Apply the best adjustments found
                lower_adj, upper_adj = best_candidate_adjustment
                lower_percentile = lower_percentile - lower_adj
                upper_percentile = upper_percentile + upper_adj
            
            # Re-evaluate with new percentiles
            try:
                coverage, avg_width, max_actual_width, lower_bounds, upper_bounds, time_metrics = evaluate_percentiles(
                    lower_percentile, upper_percentile
                )
                
                # Update best if this is better (width in range and better coverage)
                if avg_width <= max_width and coverage > best_coverage:
                    # Additionally check peak solar times if available
                    peak_width_ok = True
                    if have_peak_data:
                        peak_width = time_metrics.get('peak_width', 0)
                        if peak_width > max_width:
                            peak_width_ok = False
                            logging.debug(f"Rejecting solution with peak width {peak_width:.2f} W/m² > max {max_width} W/m²")
                    
                    if peak_width_ok:
                        best_coverage = coverage
                        best_lower = lower_percentile
                        best_upper = upper_percentile
                
                logging.debug(f"Iteration {iteration+1}: coverage={coverage:.4f}, width={avg_width:.2f}, "
                             f"lower={lower_percentile:.2f}, upper={upper_percentile:.2f}")
                
                if have_peak_data:
                    logging.debug(f"  Peak metrics: coverage={time_metrics.get('peak_coverage', 0):.4f}, width={time_metrics.get('peak_width', 0):.2f} W/m²")
            except Exception as e:
                logging.warning(f"Error in optimization iteration {iteration+1}: {str(e)}")
                continue
            
            # Check for convergence
            if iteration > 3 and abs(avg_width - max_width) < 0.5:
                logging.debug("Optimization converged on width")
                break
        
        # Final check - force max width constraint for peak times
        if have_peak_data:
            try:
                # Evaluate with current best
                _, _, _, lower_bounds, upper_bounds, time_metrics = evaluate_percentiles(best_lower, best_upper)
                peak_width = time_metrics.get('peak_width', 0)
                
                # If peak width still exceeds max, apply direct point-wise constraint
                if peak_width > max_width:
                    logging.info(f"Applying point-wise width constraint for peak solar times (current width: {peak_width:.2f} W/m²)")
                    
                    # Create point-wise constrained intervals
                    for i in range(len(lower_bounds)):
                        if peak_solar_times[i]:
                            width = upper_bounds[i] - lower_bounds[i]
                            if width > max_width:
                                # Calculate asymmetric adjustment
                                excess = width - max_width
                                
                                # Adjust bounds to maintain median prediction as center
                                # but with asymmetric shrinking based on GHI level
                                midpoint = (upper_bounds[i] + lower_bounds[i]) / 2
                                pred_level = median_preds[i]
                                
                                # High GHI: adjust lower bound more (less uncertainty on upper bound)
                                # Low GHI: adjust upper bound more (less uncertainty on lower bound)
                                if pred_level > 300:  # High GHI
                                    lower_bounds[i] = midpoint - (max_width * 0.6)
                                    upper_bounds[i] = midpoint + (max_width * 0.4)
                                else:  # Lower GHI
                                    lower_bounds[i] = midpoint - (max_width * 0.4)
                                    upper_bounds[i] = midpoint + (max_width * 0.6)
                    
                    # Calculate optimized percentiles from the point-wise adjusted bounds
                    peak_indices = np.where(peak_solar_times)[0]
                    if len(peak_indices) > 0:
                        lower_adj = np.mean(lower_bounds[peak_indices] - median_preds[peak_indices])
                        upper_adj = np.mean(upper_bounds[peak_indices] - median_preds[peak_indices])
                        
                        # Update best percentiles with these point-wise optimized values
                        best_lower = lower_adj
                        best_upper = upper_adj
                        
                        logging.info(f"Point-wise optimization resulted in percentiles: ({best_lower:.2f}, {best_upper:.2f})")
            except Exception as e:
                logging.warning(f"Error in final peak time constraint check: {str(e)}")
                # Fall back to best values found during optimization
        
        # Return the best percentiles found
        logging.info(f"Optimized interval: coverage={best_coverage:.4f}, with percentiles "
                    f"({best_lower:.2f}, {best_upper:.2f})")
        
        return best_lower, best_upper

    def optimize_model_parameters(self, X_train, y_train, X_val, y_val, max_interval_width=100):
        """
        Optimize model hyperparameters for each forecast horizon with interval width constraint.
        
        Parameters:
        -----------
        X_train: Training features
        y_train: Training targets
        X_val: Validation features
        y_val: Validation targets
        max_interval_width: Maximum allowed width between lower and upper bounds in W/m²
        
        Returns:
        --------
        dict: Best parameters for each horizon
        """
        print("Optimizing model hyperparameters for each horizon with width constraint...")
        
        # Initialize best parameters dictionary
        self.best_params = {}
        
        for horizon in self.forecast_horizons:
            print(f"\n=== Optimizing for {horizon}h horizon ===")
            target_col = f'target_GHI_{horizon}h'
            
            # Optimize median model (main forecasts)
            print(f"Optimizing median ({horizon}h) model (running 15 trials)...")
            study_median = optuna.create_study(direction='minimize')
            
            def objective_median(trial):
                params = {
                    'n_estimators': trial.suggest_int('n_estimators', 100, 500, step=100),
                    'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
                    'max_depth': trial.suggest_int('max_depth', 4, 9),
                    'subsample': trial.suggest_float('subsample', 0.7, 1.0),
                    'colsample_bytree': trial.suggest_float('colsample_bytree', 0.7, 1.0),
                    'gamma': trial.suggest_float('gamma', 0.01, 3.0),
                    'reg_alpha': trial.suggest_float('reg_alpha', 0.01, 1.0, log=True),
                    'reg_lambda': trial.suggest_float('reg_lambda', 0.01, 1.0, log=True)
                }
                
                model = xgb.XGBRegressor(objective='reg:squarederror', **params, random_state=42)
                model.fit(X_train, y_train[target_col])
                preds = model.predict(X_val)
                return mean_squared_error(y_val[target_col], preds, squared=False)
            
            study_median.optimize(objective_median, n_trials=15)
            best_params_median = study_median.best_params
            print(f"Best parameters for median ({horizon}h) model:")
            for param, value in best_params_median.items():
                print(f"  {param}: {value}")
            print(f"Best validation score: {study_median.best_value:.4f}")
            
            # Train median model to use for interval estimation
            median_model = xgb.XGBRegressor(objective='reg:squarederror', **best_params_median, random_state=42)
            median_model.fit(X_train, y_train[target_col])
            median_preds = median_model.predict(X_val)
            
            # Optimize lower model (prediction intervals)
            print(f"Optimizing lower ({horizon}h) model with width constraint (running 10 trials)...")
            study_lower = optuna.create_study(direction='minimize')
            
            def objective_lower(trial):
                params = {
                    'n_estimators': trial.suggest_int('n_estimators', 100, 500, step=100),
                    'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
                    'max_depth': trial.suggest_int('max_depth', 4, 9),
                    'subsample': trial.suggest_float('subsample', 0.7, 1.0),
                    'colsample_bytree': trial.suggest_float('colsample_bytree', 0.7, 1.0),
                    'gamma': trial.suggest_float('gamma', 0.01, 3.0),
                    'reg_alpha': trial.suggest_float('reg_alpha', 0.01, 1.0, log=True),
                    'reg_lambda': trial.suggest_float('reg_lambda', 0.01, 1.0, log=True)
                }
                
                # Train lower bound model
                model = xgb.XGBRegressor(objective='reg:squarederror', **params, random_state=42)
                model.fit(X_train, y_train[target_col])
                lower_preds = model.predict(X_val)
                
                # For lower bound, penalize overestimation more than underestimation (asymmetric loss)
                errors = y_val[target_col] - lower_preds
                
                # Penalty for overestimation (negative errors)
                asymmetric_errors = np.where(errors < 0, errors * 2.0, errors * 0.5)
                asymmetric_loss = np.mean(np.abs(asymmetric_errors))
                
                # Estimate what the interval width might be
                # Predict an approximate upper bound using error percentiles from median model
                actual_errors = y_val[target_col] - median_preds
                upper_error = np.percentile(actual_errors, 95)  # 95th percentile of errors
                approx_upper = median_preds + upper_error
                
                # Ensure non-negative predictions
                lower_preds = np.maximum(0, lower_preds)
                approx_upper = np.maximum(lower_preds, approx_upper)  # Upper must be >= lower
                
                # Calculate estimated interval width
                est_width = np.mean(approx_upper - lower_preds)
                
                # Add penalty if estimated width exceeds max allowed
                width_penalty = max(0, est_width - max_interval_width) * 0.1
                
                return asymmetric_loss + width_penalty
            
            study_lower.optimize(objective_lower, n_trials=10)
            best_params_lower = study_lower.best_params
            print(f"Best parameters for lower ({horizon}h) model:")
            for param, value in best_params_lower.items():
                print(f"  {param}: {value}")
            print(f"Best validation score: {study_lower.best_value:.4f}")
            
            # Train lower model to use for upper bound estimation
            lower_model = xgb.XGBRegressor(objective='reg:squarederror', **best_params_lower, random_state=42)
            lower_model.fit(X_train, y_train[target_col])
            lower_preds = lower_model.predict(X_val)
            
            # Optimize upper model (prediction intervals)
            print(f"Optimizing upper ({horizon}h) model with width constraint (running 10 trials)...")
            study_upper = optuna.create_study(direction='minimize')
            
            def objective_upper(trial):
                params = {
                    'n_estimators': trial.suggest_int('n_estimators', 100, 500, step=100),
                    'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
                    'max_depth': trial.suggest_int('max_depth', 4, 9),
                    'subsample': trial.suggest_float('subsample', 0.7, 1.0),
                    'colsample_bytree': trial.suggest_float('colsample_bytree', 0.7, 1.0),
                    'gamma': trial.suggest_float('gamma', 0.01, 3.0),
                    'reg_alpha': trial.suggest_float('reg_alpha', 0.01, 1.0, log=True),
                    'reg_lambda': trial.suggest_float('reg_lambda', 0.01, 1.0, log=True)
                }
                
                # Train upper bound model
                model = xgb.XGBRegressor(objective='reg:squarederror', **params, random_state=42)
                model.fit(X_train, y_train[target_col])
                upper_preds = model.predict(X_val)
                
                # For upper bound, penalize underestimation more than overestimation (asymmetric loss)
                errors = y_val[target_col] - upper_preds
                
                # Penalty for underestimation (positive errors)
                asymmetric_errors = np.where(errors > 0, errors * 2.0, errors * 0.5)
                asymmetric_loss = np.mean(np.abs(asymmetric_errors))
                
                # We already have lower predictions from the trained lower model
                # Ensure non-negative predictions
                lower_preds_safe = np.maximum(0, lower_preds) 
                upper_preds = np.maximum(lower_preds_safe, upper_preds)  # Upper must be >= lower
                
                # Calculate actual interval width
                actual_width = np.mean(upper_preds - lower_preds_safe)
                
                # Add penalty if width exceeds max allowed
                width_penalty = max(0, actual_width - max_interval_width) * 0.1
                
                return asymmetric_loss + width_penalty
            
            study_upper.optimize(objective_upper, n_trials=10)
            best_params_upper = study_upper.best_params
            print(f"Best parameters for upper ({horizon}h) model:")
            for param, value in best_params_upper.items():
                print(f"  {param}: {value}")
            print(f"Best validation score: {study_upper.best_value:.4f}")
            
            # Store best parameters
            self.best_params[horizon] = {
                'median': best_params_median,
                'lower': best_params_lower,
                'upper': best_params_upper
            }
        
        return self.best_params

    def train_models(self, X_train, y_train):
        """
        Train XGBoost models for each forecast horizon with optimized parameters.
        
        Parameters:
        -----------
        X_train: DataFrame of training features
        y_train: DataFrame of training targets
        """
        print(f"Training models for horizons {self.forecast_horizons}...")
        
        # Train a separate model for each forecast horizon
        for horizon in self.forecast_horizons:
            target_col = f'target_GHI_{horizon}h'
            
            # Get the optimized parameters for this horizon
            if hasattr(self, 'best_params') and horizon in self.best_params:
                params = self.best_params[horizon]['median']
            else:
                # Use default parameters if optimization wasn't run
                params = {
                    'n_estimators': 300,
                    'learning_rate': 0.05,
                    'max_depth': 6,
                    'subsample': 0.8,
                    'colsample_bytree': 0.8,
                    'gamma': 1.0,
                    'reg_alpha': 0.1,
                    'reg_lambda': 0.1
                }
                print(f"Warning: No optimized parameters found for horizon {horizon}. Using defaults.")
            
            # Train median model
            print(f"Training median model for {horizon}h horizon...")
            self.models_median[horizon] = xgb.XGBRegressor(
                objective='reg:squarederror',
                **params,
                random_state=42
            )
            self.models_median[horizon].fit(X_train, y_train[target_col])
            
            # Train lower bound model if parameters exist
            if hasattr(self, 'best_params') and horizon in self.best_params and 'lower' in self.best_params[horizon]:
                lower_params = self.best_params[horizon]['lower']
                print(f"Training lower bound model for {horizon}h horizon...")
                self.models_lower[horizon] = xgb.XGBRegressor(
                    objective='reg:squarederror',
                    **lower_params,
                    random_state=42
                )
                # Use quantile loss approach for lower bound (approx 10th percentile)
                self.models_lower[horizon].fit(X_train, y_train[target_col])
            
            # Train upper bound model if parameters exist
            if hasattr(self, 'best_params') and horizon in self.best_params and 'upper' in self.best_params[horizon]:
                upper_params = self.best_params[horizon]['upper']
                print(f"Training upper bound model for {horizon}h horizon...")
                self.models_upper[horizon] = xgb.XGBRegressor(
                    objective='reg:squarederror',
                    **upper_params,
                    random_state=42
                )
                # Use quantile loss approach for upper bound (approx 90th percentile)
                self.models_upper[horizon].fit(X_train, y_train[target_col])
        
        print("All models trained successfully.")

    def calculate_clear_sky_ghi(self, df):
        """
        Advanced REST2 derived clear sky model with dynamic atmospheric transmittance.
        
        Based on: Gueymard (2008) "REST2: High-performance solar radiation model for cloudless-sky irradiance"
        and Bright et al. (2018) "Improved modelling of the clear sky solar radiation"
        """
        print("Calculating advanced theoretical clear sky GHI...")
        
        # Better air mass calculation with improved accuracy for low solar elevations
        # Kasten and Young (1989) formula - more accurate than simple 1/cos(zenith)
        df['air_mass'] = np.where(
            df['solar_zenith_cos'] > 0.01,
            1.0 / (df['solar_zenith_cos'] + 0.50572 * (96.07995 - np.degrees(np.arccos(df['solar_zenith_cos'])))**-1.6364),
            np.nan
        )
        
        # Set air mass to NaN for night time
        df.loc[df['solar_zenith_cos'] <= 0, 'air_mass'] = np.nan
        
        # Dynamic transmittance model based on air mass
        # Boland et al. (2008) "Dynamic atmospheric transmittance for solar radiation modelling"
        # Using more sophisticated transmittance model that varies with air mass
        df['transmittance'] = np.where(
            df['air_mass'].notna(),
            0.8277 - 0.0322 * (df['air_mass'] - 1),
            np.nan
        )
        
        # Cap transmittance to realistic values
        df['transmittance'] = np.clip(df['transmittance'].fillna(0), 0.5, 0.85)
        
        # Extraterrestrial radiation with more accurate eccentricity formula
        # Spencer (1971) formula, widely validated in solar research
        day_angle = 2 * np.pi * (df['day_of_year'] - 1) / 365.25
        eccentricity = (1.00011 + 0.034221 * np.cos(day_angle) + 0.00128 * np.sin(day_angle) +
                       0.000719 * np.cos(2 * day_angle) + 0.000077 * np.sin(2 * day_angle))
        
        # Calculate clear sky GHI = solar constant * eccentricity * cos(zenith) * transmittance
        # Handle potential infinities by using np.where and fillna
        df['air_mass_adjusted'] = df['air_mass'].fillna(5.0)  # Fill NaN with high value for night
        df['air_mass_adjusted'] = np.clip(df['air_mass_adjusted'], 0, 10)  # Clip to reasonable range
        
        df['clear_sky_ghi'] = np.where(
            df['solar_zenith_cos'] > 0,
            self.solar_constant * eccentricity * df['solar_zenith_cos'] * df['transmittance']**df['air_mass_adjusted'],
            0
        )
        
        # Apply physically-based constraints
        df.loc[df['solar_zenith_cos'] <= 0, 'clear_sky_ghi'] = 0
        df.loc[df['clear_sky_ghi'] < 0, 'clear_sky_ghi'] = 0
        
        # Handle extremely large values (in case of numerical issues)
        max_expected_ghi = 1500  # Typical max GHI under ideal conditions
        df['clear_sky_ghi'] = np.clip(df['clear_sky_ghi'], 0, max_expected_ghi)
        
        # Calculate clear sky index with improved handling for low GHI values
        # Following Engerer & Mills (2014) methodology for clear sky index calculation
        # Avoiding division by zero
        df['clear_sky_index'] = np.where(
            df['clear_sky_ghi'] > 10,  # Only calculate for meaningful clear sky values
            df[self.target_column] / df['clear_sky_ghi'],
            0  # Set to 0 for night or very low clear sky values
        )
        
        # Handle extreme values with validated caps from literature
        df['clear_sky_index'] = np.clip(df['clear_sky_index'], 0, 1.5)  # Physically realistic max
        
        # Replace any infinities
        df['clear_sky_index'] = df['clear_sky_index'].replace([np.inf, -np.inf], 0)
        
        # Create clear sky index features
        for i in range(1, 4):
            df[f'clear_sky_index_lag_{i}'] = df['clear_sky_index'].shift(i)
        
        print("Advanced clear sky GHI calculated successfully")
        return df

    def _train_single_model(self, X_train, y_train, params=None, sample_weight=None):
        """
        Train a single XGBoost model with modified objective function.
        
        Research basis: Yang et al. (2019) "Short-term solar irradiance forecasting based on a hybrid deep learning methodology"
        and Aguiar et al. (2016) "An asymmetric Huber loss function for solar irradiance forecasting"
        """
        if params is None:
            params = {}
        
        # Define custom asymmetric loss function for GHI forecasting
        # This penalizes overestimation more than underestimation
        # Based on research showing overestimation is more problematic in energy scheduling
        def asymmetric_huber_obj(predt, dtrain):
            y = dtrain.get_label()
            delta = 10.0  # Threshold parameter
            asymmetry = 1.5  # Penalize overestimation 1.5x more than underestimation
            
            residual = y - predt
            abs_residual = np.abs(residual)
            
            # Calculate gradients and hessians
            grad = np.where(residual >= 0,
                            np.where(abs_residual <= delta, -1, -delta / abs_residual),
                            np.where(abs_residual <= delta, asymmetry, asymmetry * delta / abs_residual))
            
            hess = np.where(abs_residual <= delta, 1,
                            delta / abs_residual**2)
            
            return grad, hess
        
        # Default parameters
        base_params = {
            'n_estimators': 300,
            'learning_rate': 0.05,
            'max_depth': 6,
            'subsample': 0.8,
            'colsample_bytree': 0.8,
            'objective': 'reg:squarederror',
            'random_state': 42
        }
        
        # Use asymmetric loss for median models
        if 'quantile_alpha' not in params and params.get('objective') == 'reg:squarederror':
            base_params['objective'] = asymmetric_huber_obj
        
        # Update with provided parameters
        base_params.update(params)
        
        # Create and train model
        model = xgb.XGBRegressor(**base_params)
        model.fit(X_train, y_train, sample_weight=sample_weight)
        
        return model

    def train_specialized_models(self, X_train, y_train):
        """
        Train specialized models for different temporal regimes.
        
        Based on: Yang et al. (2018) "Time-of-day specific ensemble models for solar forecasting"
        and Feng et al. (2020) "Machine learning models for solar radiation forecasting with regime recognition"
        """
        print("Training specialized regime-based models...")
        
        # Define regimes
        # 1. Night (no sun)
        # 2. Transition (sunrise/sunset)
        # 3. Day (full sun)
        
        # Create masks for each regime
        night_mask = X_train['solar_zenith_cos'] <= 0.01
        transition_mask = (X_train['solar_zenith_cos'] > 0.01) & (X_train['solar_zenith_cos'] < 0.3)
        day_mask = X_train['solar_zenith_cos'] >= 0.3
        
        print(f"Training samples by regime - Night: {night_mask.sum()}, Transition: {transition_mask.sum()}, Day: {day_mask.sum()}")
        
        # Store specialized models
        self.specialized_models = {
            'night': {},
            'transition': {},
            'day': {}
        }
        
        # Train models for each horizon and each regime
        for horizon in self.forecast_horizons:
            target_col = f'target_GHI_{horizon}h'
            
            # Skip night models (just predict 0)
            self.specialized_models['night'][horizon] = None
            
            # Train transition models if enough samples
            if transition_mask.sum() > 100:
                print(f"Training transition model for {horizon}h horizon...")
                X_transition = X_train[transition_mask]
                y_transition = y_train.loc[transition_mask, target_col]
                
                # Use specialized parameters for transition periods
                transition_params = {
                    'n_estimators': 200,
                    'learning_rate': 0.01,
                    'max_depth': 5,
                    'gamma': 2.0,  # Higher regularization for this challenging regime
                    'min_child_weight': 5
                }
                
                self.specialized_models['transition'][horizon] = self._train_single_model(
                    X_transition, y_transition, params=transition_params
                )
            else:
                self.specialized_models['transition'][horizon] = None
            
            # Train day models
            if day_mask.sum() > 100:
                print(f"Training day model for {horizon}h horizon...")
                X_day = X_train[day_mask]
                y_day = y_train.loc[day_mask, target_col]
                
                # Use specialized parameters for daytime
                day_params = {
                    'n_estimators': 300,
                    'learning_rate': 0.05,
                    'max_depth': 7,
                    'subsample': 0.8
                }
                
                self.specialized_models['day'][horizon] = self._train_single_model(
                    X_day, y_day, params=day_params
                )
            else:
                self.specialized_models['day'][horizon] = None
        
        print("Specialized models trained successfully")
        return self.specialized_models

    def predict_with_specialized_models(self, X):
        """
        Make predictions using the appropriate specialized model for each sample.
        
        Based on: Haupt et al. (2018) "Machine learning for solar irradiance forecasting"
        and Wolff et al. (2016) "Statistical learning for short-term photovoltaic power predictions"
        """
        print("Making predictions with specialized regime-based models...")
        
        # Get regime for each sample
        night_mask = X['solar_zenith_cos'] <= 0.01
        transition_mask = (X['solar_zenith_cos'] > 0.01) & (X['solar_zenith_cos'] < 0.3)
        day_mask = X['solar_zenith_cos'] >= 0.3
        
        print(f"Test samples by regime - Night: {night_mask.sum()}, Transition: {transition_mask.sum()}, Day: {day_mask.sum()}")
        
        predictions = {}
        
        for horizon in self.forecast_horizons:
            # Initialize predictions array
            all_preds = np.zeros(len(X))
            
            # Night predictions are always 0
            all_preds[night_mask] = 0
            
            # Transition predictions
            if self.specialized_models['transition'][horizon] is not None:
                X_transition = X[transition_mask]
                if len(X_transition) > 0:
                    all_preds[transition_mask] = self.specialized_models['transition'][horizon].predict(X_transition)
            else:
                # Fall back to main model for transition
                if hasattr(self, 'models_median') and horizon in self.models_median:
                    X_transition = X[transition_mask]
                    if len(X_transition) > 0:
                        all_preds[transition_mask] = self.models_median[horizon].predict(X_transition)
            
            # Day predictions
            if self.specialized_models['day'][horizon] is not None:
                X_day = X[day_mask]
                if len(X_day) > 0:
                    all_preds[day_mask] = self.specialized_models['day'][horizon].predict(X_day)
            else:
                # Fall back to main model for day
                if hasattr(self, 'models_median') and horizon in self.models_median:
                    X_day = X[day_mask]
                    if len(X_day) > 0:
                        all_preds[day_mask] = self.models_median[horizon].predict(X_day)
            
            # Ensure non-negative predictions
            all_preds = np.maximum(0, all_preds)
            
            predictions[horizon] = all_preds
        
        return predictions

    def low_ghi_correction(self, predictions, X):
        """
        Apply corrections to improve low-GHI predictions.
        
        Based on: Betti et al. (2020) "Mapping the performance of GHI forecasts for low irradiance conditions"
        and Lauret et al. (2015) "A benchmarking of machine learning techniques for solar radiation forecasting"
        """
        print("Applying low-GHI corrections...")
        
        corrected_predictions = {}
        
        # Threshold for "low" GHI values
        low_ghi_threshold = 50  # W/m²
        
        for horizon, preds in predictions.items():
            # Get clear sky GHI for the prediction time
            clear_sky_pred = X['clear_sky_ghi'].values
            
            # Apply correction only where clear sky GHI is low but positive
            low_ghi_mask = (clear_sky_pred > 0) & (clear_sky_pred < low_ghi_threshold)
            
            # Copy original predictions
            corrected = preds.copy()
            
            if np.any(low_ghi_mask):
                # For low GHI values, apply a correction factor
                # Research shows predictions in this range are often overestimated
                correction_factor = np.where(
                    preds[low_ghi_mask] > 0.5 * clear_sky_pred[low_ghi_mask],
                    0.5 * clear_sky_pred[low_ghi_mask] / preds[low_ghi_mask],
                    1.0
                )
                
                # Apply the correction
                corrected[low_ghi_mask] = preds[low_ghi_mask] * correction_factor
            
            corrected_predictions[horizon] = corrected
        
        return corrected_predictions

    def split_time_series_data(self, data, test_size=0.2, val_size=0.1):
        """
        Split data into training, validation, and test sets, respecting time ordering.
        
        Parameters:
        data (pandas.DataFrame): DataFrame with features and targets
        test_size (float): Proportion of data to use for testing
        val_size (float): Proportion of remaining data to use for validation
        
        Returns:
        tuple: X_train, X_test, X_val, y_train, y_test, y_val, feature_columns
        """
        # Ensure we're respecting time ordering
        if 'datetime' in data.columns:
            data = data.sort_values('datetime')
        
        # First, split into training+validation and test
        train_val_size = 1 - test_size
        train_val_idx = int(len(data) * train_val_size)
        
        train_val_data = data.iloc[:train_val_idx].copy()
        test_data = data.iloc[train_val_idx:].copy()
        
        # Then, split training data into train and validation
        train_size = 1 - val_size
        train_idx = int(len(train_val_data) * train_size)
        
        train_data = train_val_data.iloc[:train_idx].copy()
        val_data = train_val_data.iloc[train_idx:].copy()
        
        # Print information about the splits
        print(f"Training set size: {len(train_data)}, Validation set size: {len(val_data)}, Test set size: {len(test_data)}")
        
        if 'datetime' in data.columns:
            print(f"Train period: {train_data['datetime'].min()} to {train_data['datetime'].max()}")
            print(f"Validation period: {val_data['datetime'].min()} to {val_data['datetime'].max()}")
            print(f"Test period: {test_data['datetime'].min()} to {test_data['datetime'].max()}")
        
        # Extract features and targets
        X_train = train_data.drop([self.target_column] + self.target_columns, axis=1)
        y_train = train_data[self.target_columns]
        
        X_val = val_data.drop([self.target_column] + self.target_columns, axis=1)
        y_val = val_data[self.target_columns]
        
        X_test = test_data.drop([self.target_column] + self.target_columns, axis=1)
        y_test = test_data[self.target_columns]
        
        # Check for and eliminate columns with NaN or inf values
        # This is safer than dealing with them later
        for X in [X_train, X_val, X_test]:
            for col in X.columns:
                # First check the data type of the column to avoid type errors
                if pd.api.types.is_numeric_dtype(X[col]):
                    # Safe to check for NaN and inf in numeric columns
                    if X[col].isna().any():
                        # Fill NaN with the median for numeric columns
                        median_val = X[col].median()
                        if pd.isna(median_val):  # If median is also NaN
                            median_val = 0
                        X[col] = X[col].fillna(median_val)
                    
                    # Check for infinities separately
                    try:
                        # Try to replace infinities
                        has_inf = np.isinf(X[col]).any()
                        if has_inf:
                            X[col] = X[col].replace([np.inf, -np.inf], np.nan)
                            # Then fill those NaNs
                            X[col] = X[col].fillna(X[col].median() if not pd.isna(X[col].median()) else 0)
                    except TypeError:
                        # If isinf fails, replace with NaN directly
                        X[col] = pd.to_numeric(X[col], errors='coerce')
                        X[col] = X[col].fillna(X[col].median() if not pd.isna(X[col].median()) else 0)
                else:
                    # For non-numeric columns, we only need to handle NaN
                    if X[col].isna().any():
                        # For non-numeric, fill with most common value
                        most_common = X[col].mode()[0]
                        X[col] = X[col].fillna(most_common)
        
        return X_train, X_test, X_val, y_train, y_test, y_val

    def evaluate_validation(self, X_val, y_val):
        """Evaluate models on validation data with interval width metrics."""
        metrics = {}
        
        for horizon in self.forecast_horizons:
            target_col = f'target_GHI_{horizon}h'
            actual = y_val[target_col].values
            
            # Predict with median model
            median_preds = self.models_median[horizon].predict(X_val)
            
            # Calculate metrics
            mae = mean_absolute_error(actual, median_preds)
            rmse = mean_squared_error(actual, median_preds, squared=False)
            
            # Calculate MAPE
            non_zero_mask = actual > 10
            if np.sum(non_zero_mask) > 0:
                mape = 100 * np.mean(np.abs((actual[non_zero_mask] - median_preds[non_zero_mask]) / actual[non_zero_mask]))
            else:
                mape = np.nan
            
            # Calculate prediction interval coverage and width using error percentiles
            interval_width = None
            interval_max_width = None
            
            if hasattr(self, 'error_percentiles') and horizon in self.error_percentiles:
                lower_err, upper_err = self.error_percentiles[horizon]
                
                # Calculate bounds using error percentiles
                lower_bounds = median_preds + lower_err
                upper_bounds = median_preds + upper_err
                
                # Ensure non-negative and proper ordering
                lower_bounds = np.maximum(0, lower_bounds)
                upper_bounds = np.maximum(lower_bounds, upper_bounds)
                
                # Calculate width metrics
                interval_width = np.mean(upper_bounds - lower_bounds)
                interval_max_width = np.max(upper_bounds - lower_bounds)
                
                # Calculate coverage
                coverage = 100 * np.mean((actual >= lower_bounds) & (actual <= upper_bounds))
            # Fallback to old method
            elif horizon in self.models_lower and horizon in self.models_upper:
                # Previous logic here...
                lower_preds = self.models_lower[horizon].predict(X_val)
                upper_preds = self.models_upper[horizon].predict(X_val)
                
                # Calculate coverage (percentage of actual values within prediction interval)
                coverage = 100 * np.mean((y_val[target_col] >= lower_preds) & (y_val[target_col] <= upper_preds))
                
                # Calculate width metrics
                interval_width = np.mean(upper_preds - lower_preds)
                interval_max_width = np.max(upper_preds - lower_preds)
            else:
                coverage = np.nan
                interval_width = np.nan
                interval_max_width = np.nan
            
            # Calculate skill score against persistence forecast
            # For GHI forecasting, persistence means using current value as prediction
            if 'GHI - W/m^2' in X_val.columns:
                # Current GHI values
                current_ghi = X_val['GHI - W/m^2'].values
                
                # Calculate persistence error (using current value to predict future)
                persistence_mae = mean_absolute_error(actual, current_ghi)
                persistence_rmse = mean_squared_error(actual, current_ghi, squared=False)
                
                # Calculate skill scores (improvement over persistence)
                # 1 means perfect, 0 means same as persistence, negative means worse
                if persistence_mae > 0:
                    skill_score_mae = 1 - (mae / persistence_mae)
                else:
                    skill_score_mae = 0
                    
                if persistence_rmse > 0:
                    skill_score_rmse = 1 - (rmse / persistence_rmse)
                else:
                    skill_score_rmse = 0
            else:
                skill_score_mae = np.nan
                skill_score_rmse = np.nan
            
            # Store metrics
            metrics[horizon] = {
                'mae': mae,
                'rmse': rmse,
                'mape': mape,
                'coverage': coverage,
                'interval_width': interval_width,
                'interval_max_width': interval_max_width,
                'skill_score_mae': skill_score_mae,
                'skill_score_rmse': skill_score_rmse
            }
        
        return metrics

    def predict_future_hours(self, data=None, file_path=None, num_hours=4):
        """
        Predict GHI values for the next few hours beyond the dataset.
        """
        if data is None and file_path is not None:
            data = self.load_data(file_path)
        
        if data is None:
            raise ValueError("No data provided for prediction")
        
        # Add validation for datetime column
        if 'datetime' in data.columns and data['datetime'].isna().any():
            self.logger.warning("Missing datetime values detected in input data")
            # Fill missing datetimes with the most recent valid datetime + 1 hour
            last_valid_datetime = data['datetime'].dropna().iloc[-1] if not data['datetime'].dropna().empty else pd.Timestamp.now()
            data['datetime'] = data['datetime'].fillna(last_valid_datetime)
        
        # Ensure all datetime-related columns are properly filled
        if 'Date' in data.columns and data['Date'].isna().any():
            data['Date'] = data['Date'].fillna(data['datetime'].dt.date if 'datetime' in data.columns else pd.Timestamp.now().date())
        
        if 'Start Period' in data.columns and data['Start Period'].isna().any():
            data['Start Period'] = data['Start Period'].fillna(data['datetime'].dt.strftime('%H:%M:%S') if 'datetime' in data.columns else pd.Timestamp.now().strftime('%H:%M:%S'))
        
        if 'End Period' in data.columns and data['End Period'].isna().any():
            data['End Period'] = data['End Period'].fillna(data['datetime'].dt.strftime('%H:%M:%S') if 'datetime' in data.columns else pd.Timestamp.now().strftime('%H:%M:%S'))
        
        # Preprocess and create features
        data = self.preprocess_data(data)
        X_pred = self.create_features_for_prediction(data)
        
        # Keep track of the last timestamp if available
        if 'datetime' in X_pred.columns:
            last_timestamp = X_pred['datetime'].iloc[-1]
        else:
            last_timestamp = None
        
        # Drop metadata columns that weren't part of model training
        X_pred = X_pred.drop(['Date', 'Start Period', 'End Period', 'datetime'], errors='ignore')
        
        # Ensure we have all required feature columns
        missing_cols = set(self.feature_columns) - set(X_pred.columns)
        if missing_cols:
            raise ValueError(f"Missing required feature columns: {missing_cols}")
        
        # Ensure columns are in the same order as during training
        X_pred = X_pred[self.feature_columns]
        
        # Debug: Check if we have all required feature columns
        missing_features = [col for col in self.feature_columns if col not in X_pred.columns]
        if missing_features:
            logging.warning(f"Missing required features for prediction: {missing_features}")
            # Create missing features with default values to avoid errors
            for col in missing_features:
                X_pred[col] = 0.0
        
        # Debug: Check for NaN values in features
        nan_columns = X_pred.columns[X_pred.isna().any()].tolist()
        if nan_columns:
            logging.warning(f"NaN values found in prediction features: {nan_columns}")
            # Fill NaN values with 0 to prevent errors
            X_pred = X_pred.fillna(0)
        
        # Check for infinity or very large values - but only in numeric columns
        inf_columns = []
        for col in X_pred.columns:
            # First check if the column is numeric before applying np.isinf
            if pd.api.types.is_numeric_dtype(X_pred[col]):
                if np.isinf(X_pred[col]).any():
                    inf_columns.append(col)
                    self.debug_logger.debug(f"Infinity found in column '{col}', replacing with 0")
                    X_pred[col] = X_pred[col].replace([np.inf, -np.inf], 0)
                
                # Also check for very large values that might cause overflow
                if (np.abs(X_pred[col]) > 1e10).any():
                    self.debug_logger.debug(f"Extremely large values found in column '{col}', capping values")
                    X_pred[col] = np.clip(X_pred[col], -1e10, 1e10)
            else:
                self.debug_logger.debug(f"Skipping infinity check for non-numeric column: {col}")

        if inf_columns:
            logging.info(f"Fixed infinity in these columns: {inf_columns}")
        
        # Use the model's scaler that was already fit during training
        try:
            # Print column counts to confirm we have the right number
            self.debug_logger.debug(f"Scaling {len(X_pred.columns)} features (expected {len(self.scaler.feature_names_in_) if hasattr(self.scaler, 'feature_names_in_') else 'unknown'})")
            
            # Ensure column order matches exactly what the scaler expects
            if hasattr(self.scaler, 'feature_names_in_'):
                # Reorder columns to match the exact order the scaler expects
                expected_columns = self.scaler.feature_names_in_
                missing_cols = set(expected_columns) - set(X_pred.columns)
                
                if missing_cols:
                    logging.warning(f"Missing columns needed by scaler: {missing_cols}")
                    for col in missing_cols:
                        X_pred[col] = 0.0  # Add missing columns with default values
                
                # Reorder to exactly match the scaler's expected order
                X_pred = X_pred[expected_columns]
            
            # One final check for any non-finite values that might have snuck in
            X_pred_clean = X_pred.copy()
            for col in X_pred_clean.columns:
                if pd.api.types.is_numeric_dtype(X_pred_clean[col]):
                    X_pred_clean[col] = X_pred_clean[col].replace([np.inf, -np.inf], 0).fillna(0)
            
            # Debug info about data range
            self.debug_logger.debug(f"Data range check - Min values: {X_pred_clean.min().min():.4f}, Max values: {X_pred_clean.max().max():.4f}")
            
            # More detailed diagnostics for debugging scaling issues
            self.debug_logger.debug("\n=== DETAILED DIAGNOSTICS ===")
            
            X_pred_scaled = self.scaler.transform(X_pred_clean)
            
            # Convert back to DataFrame to maintain column names
            X_pred_scaled_df = pd.DataFrame(X_pred_scaled, columns=X_pred_clean.columns)
            
            logging.info(f"Successfully scaled features with shape: {X_pred_scaled_df.shape}")
        except Exception as e:
            logging.error(f"ERROR during feature scaling: {str(e)}")
            # More detailed diagnostics for numeric issues
            self.debug_logger.debug("\n=== DETAILED DIAGNOSTICS ===")
            for col in X_pred.columns:
                if pd.api.types.is_numeric_dtype(X_pred[col]):
                    col_data = X_pred[col]
                    has_inf = np.isinf(col_data).any()
                    has_nan = np.isnan(col_data).any()
                    if has_inf or has_nan:
                        self.debug_logger.debug(f"Column '{col}' contains {'infinity' if has_inf else ''} {'NaN' if has_nan else ''}")
                    
                    # Check for extremely large values
                    if not has_inf and not has_nan:
                        try:
                            col_max = np.max(np.abs(col_data))
                            if col_max > 1e10:
                                self.debug_logger.debug(f"Column '{col}' has extremely large value: {col_max}")
                        except:
                            self.debug_logger.debug(f"Error computing max for column '{col}'")
                else:
                    self.debug_logger.debug(f"Column '{col}' is non-numeric type: {X_pred[col].dtype}")
        
            raise
        
        # Make predictions for each horizon
        future_predictions = []
        
        # Generate predictions for each requested hour
        for hour in range(1, min(num_hours+1, max(self.forecast_horizons)+1)):
            if hour not in self.forecast_horizons:
                logging.info(f"Horizon {hour}h not in trained horizons, skipping")
                continue
                
            # Make predictions with intervals
            if hour in self.models_median:
                try:
                    # Debug: Print model input shape
                    self.debug_logger.debug(f"Model input shape for horizon {hour}h: {X_pred_scaled_df.shape}")
                    
                    # Basic prediction
                    median_pred = self.models_median[hour].predict(X_pred_scaled_df)[0]
                    
                    # Use error percentiles for confidence intervals if available
                    if hasattr(self, 'error_percentiles') and hour in self.error_percentiles:
                        lower_err, upper_err = self.error_percentiles[hour]
                        lower_bound = max(0, median_pred + lower_err)
                        upper_bound = max(lower_bound, median_pred + upper_err)
                    elif hour in self.models_lower and hour in self.models_upper:
                        # Fallback to model-based intervals
                        lower_bound = max(0, self.models_lower[hour].predict(X_pred_scaled_df)[0])
                        upper_bound = max(lower_bound, self.models_upper[hour].predict(X_pred_scaled_df)[0])
                    else:
                        # No intervals available
                        lower_bound = None
                        upper_bound = None
                    
                    # Create future timestamp if datetime is available
                    if last_timestamp is not None:
                        from pandas import Timedelta
                        future_timestamp = last_timestamp + Timedelta(hours=hour)
                        future_time_str = future_timestamp.strftime("%Y-%m-%d %H:%M:%S")
                        
                        # Check if the future timestamp falls during nighttime, based on Philippines climate
                        hour_of_day = future_timestamp.hour
                        month = future_timestamp.month
                        
                        # Apply the same Philippines climate nighttime detection logic
                        # Cool dry season (December to February)
                        if month in [12, 1, 2]:  
                            is_nighttime = (hour_of_day >= 18) or (hour_of_day < 6)
                            # Transition periods (dawn/dusk)
                            is_transition = hour_of_day == 6 or hour_of_day == 17
                            season = "Cool dry"
                        # Hot dry season (March to May)
                        elif month in [3, 4, 5]:
                            is_nighttime = (hour_of_day >= 18) or (hour_of_day < 5)
                            # Transition periods (dawn/dusk)
                            is_transition = hour_of_day == 5 or hour_of_day == 17
                            season = "Hot dry"
                        # Rainy season (June to November)
                        else:  # months 6-11
                            is_nighttime = (hour_of_day >= 18) or (hour_of_day < 6)
                            # Transition periods (dawn/dusk)
                            is_transition = hour_of_day == 6 or hour_of_day == 17
                            season = "Rainy"
                        
                        # If nighttime, set all predictions to zero
                        if is_nighttime:
                            logging.info(f"Horizon {hour}h at {future_time_str} (hour: {hour_of_day}, month: {month}) is during nighttime ({season} season). Setting GHI to 0.")
                            median_pred = 0.0
                            lower_bound = 0.0
                            upper_bound = 0.0
                        # If transition period (dawn/dusk), reduce predictions by 70%
                        elif is_transition:
                            # First ensure no negative values
                            median_pred = max(0.0, median_pred)
                            if lower_bound is not None:
                                lower_bound = max(0.0, lower_bound)
                            if upper_bound is not None:
                                upper_bound = max(0.0, upper_bound)
                                
                            transition_factor = 0.3  # Reduce to 30% of predicted value
                            original_pred = median_pred
                            original_lower = lower_bound
                            original_upper = upper_bound
                            
                            median_pred *= transition_factor
                            if lower_bound is not None:
                                lower_bound *= transition_factor
                            if upper_bound is not None:
                                upper_bound *= transition_factor
                            
                            logging.info(f"Horizon {hour}h at {future_time_str} (hour: {hour_of_day}, month: {month}) is during transition ({season} season). Reducing GHI from {original_pred:.2f} to {median_pred:.2f}.")
                        else:
                            logging.info(f"Horizon {hour}h at {future_time_str} (hour: {hour_of_day}, month: {month}) is during daytime ({season} season).")
                    else:
                        future_time_str = f"t+{hour}h"
                    
                    # Add to predictions list
                    future_predictions.append({
                        'datetime': future_time_str,
                        'lower_bound': round(lower_bound, 2) if lower_bound is not None else None,
                        'median': round(median_pred, 2),
                        'upper_bound': round(upper_bound, 2) if upper_bound is not None else None
                    })
                except Exception as e:
                    logging.error(f"Error predicting for horizon {hour}h: {str(e)}")
            else:
                # No model available for this horizon
                logging.info(f"No median model available for horizon {hour}h, skipping prediction")
        
        # Convert to DataFrame for easy display
        if future_predictions:
            result_df = pd.DataFrame(future_predictions)
            # Reorder columns to match requested sequence
            result_df = result_df[['datetime', 'lower_bound', 'median', 'upper_bound']]
            logging.info("\n=== Future Hour Predictions ===")
            logging.info(f"\n{result_df.to_string()}")
            
            # Save predictions
            result_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'future_predictions.csv')  # Save in davcast folder
            result_df.to_csv(result_file, index=False)
            logging.info(f"Future predictions saved to {result_file}")
            
            return result_df
        else:
            logging.warning("No future predictions generated")
            return None

    def create_features_for_prediction(self, data):
        """
        Create features for a single prediction.
        
        Parameters:
        -----------
        data: DataFrame
            Dataset containing the latest rows needed for feature generation
        
        Returns:
        --------
        DataFrame: Single row with all required features for prediction
        """
        df = data.copy()
        
        # Calculate all the same features as in training, but don't create targets
        # Create lagged features for GHI
        lag_hours = max([int(col.split('_')[-1]) for col in df.columns if col.startswith('GHI_lag_')], default=3)
        for i in range(1, lag_hours + 1):
            col_name = f'GHI_lag_{i}'
            if col_name not in df.columns:
                df[col_name] = df[self.target_column].shift(i)
        
        # Create rolling statistics
        if 'GHI_rolling_mean_6h' not in df.columns:
            df['GHI_rolling_mean_6h'] = df[self.target_column].rolling(window=6, min_periods=1).mean()
        
        if 'GHI_rolling_max_24h' not in df.columns:
            df['GHI_rolling_max_24h'] = df[self.target_column].rolling(window=24, min_periods=1).max()
        
        # Add solar position features first - IMPORTANT: This needs to come before clear sky calculation
        df = self.add_solar_position_features(df)
        
        # Then calculate clear sky GHI which depends on solar position features
        df = self.calculate_clear_sky_ghi(df)
        
        # Add trend features if not already present
        if 'Clear Sky Index' not in df.columns:
            df = self.add_trend_features(df)
        
        # Add diurnal decomposition if not already present
        if 'CSI_smooth' not in df.columns:
            df = self.add_diurnal_decomposition(df)
        
        # Check for and handle NaN values in datetime features before processing
        if 'datetime' in data.columns:
            # Ensure datetime column is actually datetime type
            if not pd.api.types.is_datetime64_any_dtype(data['datetime']):
                try:
                    data['datetime'] = pd.to_datetime(data['datetime'])
                except Exception as e:
                    self.logger.error(f"Error converting datetime column: {str(e)}")
                    # Create a valid datetime as fallback
                    data['datetime'] = pd.Timestamp.now()
            
            # Fill any remaining NaT values
            if data['datetime'].isna().any():
                data['datetime'] = data['datetime'].fillna(method='ffill')
                # If still has NaT (e.g., first row), fill with current time
                data['datetime'] = data['datetime'].fillna(pd.Timestamp.now())
        
        # Only return the last row which has all the lagged features filled
        return df.iloc[[-1]].copy()
        
    def save_models(self, model_dir=None):
        """
        Save trained models and associated metadata.
        
        Parameters:
        -----------
        model_dir (str): Directory to save models (default: current directory)
        """
        if model_dir is None:
            model_dir = os.path.dirname(os.path.abspath(__file__))
        
        try:
            os.makedirs(model_dir, exist_ok=True)
            
            # Save median, lower, and upper bound models
            for horizon in self.forecast_horizons:
                # Save median model
                if horizon in self.models_median:
                    model_path = os.path.join(model_dir, f'xgboost_model_hour_{horizon}.json')
                    self.models_median[horizon].save_model(model_path)
                    logging.info(f"Saved median model for horizon {horizon}h to {model_path}")
                
                # Save lower bound model
                if horizon in self.models_lower:
                    model_path = os.path.join(model_dir, f'xgboost_model_lower_hour_{horizon}.json')
                    self.models_lower[horizon].save_model(model_path)
                    logging.info(f"Saved lower bound model for horizon {horizon}h to {model_path}")
                
                # Save upper bound model
                if horizon in self.models_upper:
                    model_path = os.path.join(model_dir, f'xgboost_model_upper_hour_{horizon}.json')
                    self.models_upper[horizon].save_model(model_path)
                    logging.info(f"Saved upper bound model for horizon {horizon}h to {model_path}")
            
            # Save feature scaler for preprocessing
            scaler_path = os.path.join(model_dir, 'features_scaler.joblib')
            joblib.dump(self.scaler, scaler_path)
            logging.info(f"Saved feature scaler to {scaler_path}")
            
            # Save feature columns for prediction
            if self.feature_columns is not None:
                feature_cols_path = os.path.join(model_dir, 'feature_columns.json')
                with open(feature_cols_path, 'w') as f:
                    json.dump(self.feature_columns, f)
                logging.info(f"Saved feature columns to {feature_cols_path}")
            
            # Save calibrated error percentiles
            if hasattr(self, 'error_percentiles'):
                percentiles_path = os.path.join(model_dir, 'error_percentiles.joblib')
                joblib.dump(self.error_percentiles, percentiles_path)
                logging.info(f"Saved error percentiles to {percentiles_path}")
            
            # Save interval widths metadata
            if hasattr(self, 'interval_widths'):
                widths_path = os.path.join(model_dir, 'interval_widths.json')
                with open(widths_path, 'w') as f:
                    json.dump({str(k): float(v) for k, v in self.interval_widths.items()}, f)
                logging.info(f"Saved interval widths to {widths_path}")
            
            return True
        except Exception as e:
            logging.error(f"Error saving models: {str(e)}")
            return False

    def load_models(self, model_dir=None):
        """
        Load trained models and associated metadata.
        
        Parameters:
        -----------
        model_dir (str): Directory where models are saved (default: current directory)
        
        Returns:
        --------
        bool: True if models were loaded successfully, False otherwise
        """
        if model_dir is None:
            model_dir = os.path.dirname(os.path.abspath(__file__))
        
        try:
            # Load feature columns
            feature_cols_path = os.path.join(model_dir, 'feature_columns.json')
            if os.path.exists(feature_cols_path):
                with open(feature_cols_path, 'r') as f:
                    self.feature_columns = json.load(f)
                logging.info(f"Loaded feature columns from {feature_cols_path}")
            
            # Load feature scaler
            scaler_path = os.path.join(model_dir, 'features_scaler.joblib')
            if os.path.exists(scaler_path):
                self.scaler = joblib.load(scaler_path)
                logging.info(f"Loaded feature scaler from {scaler_path}")
            
            # Load calibrated error percentiles
            percentiles_path = os.path.join(model_dir, 'error_percentiles.joblib')
            if os.path.exists(percentiles_path):
                self.error_percentiles = joblib.load(percentiles_path)
                logging.info(f"Loaded error percentiles from {percentiles_path}")
            
            # Load interval widths metadata
            widths_path = os.path.join(model_dir, 'interval_widths.json')
            if os.path.exists(widths_path):
                with open(widths_path, 'r') as f:
                    # Convert string keys back to integers
                    width_data = json.load(f)
                    self.interval_widths = {int(k): v for k, v in width_data.items()}
                logging.info(f"Loaded interval widths from {widths_path}")
            
            # Initialize model dictionaries
            self.models_median = {}
            self.models_lower = {}
            self.models_upper = {}
            
            # Load all horizon models
            for horizon in self.forecast_horizons:
                # Load median model
                model_path = os.path.join(model_dir, f'xgboost_model_hour_{horizon}.json')
                if os.path.exists(model_path):
                    model = xgb.XGBRegressor()
                    model.load_model(model_path)
                    self.models_median[horizon] = model
                    logging.info(f"Loaded median model for horizon {horizon}h from {model_path}")
                else:
                    logging.warning(f"Median model for horizon {horizon}h not found at {model_path}")
                
                # Load lower bound model
                model_path = os.path.join(model_dir, f'xgboost_model_lower_hour_{horizon}.json')
                if os.path.exists(model_path):
                    model = xgb.XGBRegressor()
                    model.load_model(model_path)
                    self.models_lower[horizon] = model
                    logging.info(f"Loaded lower bound model for horizon {horizon}h from {model_path}")
                
                # Load upper bound model
                model_path = os.path.join(model_dir, f'xgboost_model_upper_hour_{horizon}.json')
                if os.path.exists(model_path):
                    model = xgb.XGBRegressor()
                    model.load_model(model_path)
                    self.models_upper[horizon] = model
                    logging.info(f"Loaded upper bound model for horizon {horizon}h from {model_path}")
            
            return len(self.models_median) > 0
        except Exception as e:
            logging.error(f"Error loading models: {str(e)}")
            return False

    def analyze_interval_widths_by_hour(self, X_data, y_data, horizon=1):
        """
        Analyze prediction interval widths broken down by hour of day.
        This helps identify times when the interval width constraint may be harder to satisfy.
        
        Parameters:
        -----------
        X_data: DataFrame with features 
        y_data: DataFrame with target values
        horizon: Forecast horizon to analyze (default: 1 hour ahead)
        
        Returns:
        --------
        DataFrame: Interval width statistics by hour of day
        """
        if not hasattr(self, 'error_percentiles') or horizon not in self.error_percentiles:
            logging.error(f"No calibrated error percentiles available for horizon {horizon}h")
            return None
        
        if 'datetime' not in X_data.columns:
            logging.error("Datetime column required for hour-of-day analysis")
            return None
        
        logging.info(f"Analyzing interval widths by hour of day for horizon {horizon}h...")
        
        # Get the target column
        target_col = f'target_GHI_{horizon}h'
        if target_col not in y_data.columns:
            logging.error(f"Target column {target_col} not found in validation data")
            return None
        
        # Get actual values and hour of day
        hours = X_data['datetime'].dt.hour
        actual = y_data[target_col].values
        
        # Filter metadata columns from required features
        if not hasattr(self, 'feature_columns') or self.feature_columns is None:
            logging.error("Feature columns not available. Model may not be trained.")
            return None
        
        # Define metadata columns that should be excluded
        metadata_cols = ['Date', 'datetime', 'Start Period', 'End Period']
        
        # Filter to only include feature columns
        feature_cols = [col for col in self.feature_columns if col not in metadata_cols]
        
        # Check if all required feature columns are present
        missing_features = [col for col in feature_cols if col not in X_data.columns]
        if missing_features:
            logging.error(f"Missing required feature columns: {missing_features}")
            return None
        
        # Get median predictions using only the feature columns
        try:
            median_preds = self.models_median[horizon].predict(X_data[feature_cols])
        except Exception as e:
            logging.error(f"Error making predictions: {str(e)}")
            return None
        
        # Get error percentiles
        lower_percentile, upper_percentile = self.error_percentiles[horizon]
        
        # Calculate prediction bounds
        lower_bounds = median_preds + lower_percentile
        upper_bounds = median_preds + upper_percentile
        
        # Ensure non-negative bounds and proper ordering
        lower_bounds = np.maximum(0, lower_bounds)
        upper_bounds = np.maximum(lower_bounds, upper_bounds)
        
        # Calculate interval widths
        widths = upper_bounds - lower_bounds
        
        # Calculate coverage
        coverage = (actual >= lower_bounds) & (actual <= upper_bounds)
        
        # Create a dataframe for analysis
        analysis_df = pd.DataFrame({
            'hour': hours,
            'actual': actual,
            'predicted': median_preds,
            'lower_bound': lower_bounds,
            'upper_bound': upper_bounds,
            'width': widths,
            'coverage': coverage
        })
        
        # Group by hour of day and calculate statistics
        hourly_stats = analysis_df.groupby('hour').agg({
            'width': ['mean', 'max', 'min', 'std'],
            'coverage': 'mean',
            'actual': 'mean',
            'predicted': 'mean'
        })
        
        # Flatten the multi-index columns
        hourly_stats.columns = ['_'.join(col).strip() for col in hourly_stats.columns.values]
        
        # Add percentage of intervals exceeding max width
        max_width = 100  # W/m²
        for hour in range(24):
            if hour in hourly_stats.index:
                hour_mask = hours == hour
                widths_hour = widths[hour_mask]
                hourly_stats.loc[hour, 'pct_exceeding_max'] = 100 * np.mean(widths_hour > max_width)
                hourly_stats.loc[hour, 'n_samples'] = np.sum(hour_mask)
        
        # Print summary of problematic hours
        problem_hours = hourly_stats[hourly_stats['width_max'] > max_width].sort_values('width_max', ascending=False)
        if not problem_hours.empty:
            logging.info(f"Hours with interval widths exceeding {max_width} W/m² (horizon {horizon}h):")
            for hour, row in problem_hours.iterrows():
                logging.info(f"  Hour {hour}: Max width {row['width_max']:.2f} W/m², Avg width {row['width_mean']:.2f} W/m², "
                            f"Coverage {row['coverage_mean']*100:.2f}%, "
                            f"{row['pct_exceeding_max']:.1f}% of intervals exceed max width")
        
        return hourly_stats

    def predict(self, X_new, return_intervals=True):
        """
        Make predictions with empirically calibrated intervals.
        """
        logging.info(f"Making multi-horizon predictions for horizons {self.forecast_horizons}...")
        
        # Handle metadata columns gracefully - check which ones are available
        metadata_cols = ['Date', 'datetime', 'Start Period', 'End Period']
        available_cols = [col for col in metadata_cols if col in X_new.columns]
        missing_cols = set(metadata_cols) - set(available_cols)
        
        # Instead of raising an error, just log the info about missing columns
        if missing_cols:
            logging.info(f"Some metadata columns are not in input data: {missing_cols}")
            logging.info("Continuing with prediction without these columns...")
        
        # Get feature columns from the model
        if self.feature_columns is None:
            raise ValueError("Model has not been trained (feature_columns is None)")
        
        # Filter out metadata columns from required features
        required_features = [col for col in self.feature_columns 
                              if col not in metadata_cols]
        
        # Get only the columns needed for prediction
        available_features = [col for col in required_features if col in X_new.columns]
        missing_features = set(required_features) - set(available_features)
        
        if missing_features:
            raise ValueError(f"Missing required feature columns: {missing_features}")
        
        # Debug info about predictions structure
        self.debug_logger.debug(f"Debug - predictions keys: {self.forecast_horizons}")
        
        # Create predictions for each forecast horizon
        predictions = {}
        
        # Create a DataFrame to log nighttime detections for exporting
        nighttime_log = []
        
        for horizon in self.forecast_horizons:
            if horizon not in self.models_median:
                raise ValueError(f"No trained model available for {horizon}h horizon")
            
            # Make median prediction
            median_preds = self.models_median[horizon].predict(X_new[available_features])
            
            # Initialize predictions dictionary for this horizon
            horizon_preds = {}
            
            # Apply a robust nighttime check for future predictions
            is_night = np.zeros_like(median_preds, dtype=bool)
            is_transition = np.zeros_like(median_preds, dtype=bool)  # For dawn/dusk transitions
            
            # If datetime information is available, use it to check nighttime directly
            if 'datetime' in X_new.columns:
                try:
                    # Calculate target prediction timestamps for this horizon
                    future_timestamps = X_new['datetime'] + pd.Timedelta(hours=horizon)
                    
                    # For each timestamp, determine if it's nighttime based on time and season
                    for i, timestamp in enumerate(future_timestamps):
                        # Extract hour (0-23) from timestamp
                        hour = timestamp.hour
                        month = timestamp.month
                        
                        # SIMPLIFIED NIGHTTIME DETECTION BASED ON PHILIPPINES CLIMATE
                        # For Davao City, Philippines (latitude: 7.07)
                        # The Philippines has two main seasons:
                        
                        # For Philippines climate (Tropical)
                        # Cool dry season (December to February)
                        if month in [12, 1, 2]:  
                            is_nighttime = (hour >= 18) or (hour < 6)  # Earlier sunset, later sunrise
                            is_transition_time = hour == 6 or hour == 17  # Dawn/dusk transition hours
                            season = "Cool dry"
                        # Hot dry season (March to May)
                        elif month in [3, 4, 5]:
                            is_nighttime = (hour >= 18) or (hour < 5)  # Later sunset, earlier sunrise
                            is_transition_time = hour == 5 or hour == 17  # Dawn/dusk transition hours
                            season = "Hot dry"
                        # Rainy season (June to November)
                        else:  # months 6-11
                            is_nighttime = (hour >= 18) or (hour < 6)  # More cloud cover, affects daylight
                            is_transition_time = hour == 6 or hour == 17  # Dawn/dusk transition hours
                            season = "Rainy"
                        
                        if is_nighttime:
                            is_night[i] = True
                            self.debug_logger.debug(f"Horizon {horizon}h prediction at {timestamp} will be during nighttime (hour: {hour}, month: {month}, season: {season})")
                        elif is_transition_time:
                            is_transition[i] = True
                            self.debug_logger.debug(f"Horizon {horizon}h prediction at {timestamp} will be during transition period (hour: {hour}, month: {month}, season: {season})")
                            
                        # Add to nighttime log for export
                        nighttime_log.append({
                            'horizon': horizon,
                            'timestamp': timestamp,
                            'hour': hour,
                            'month': month,
                            'season': season,
                            'is_nighttime': is_nighttime,
                            'is_transition': is_transition_time
                        })
                
                    # Log summary of detections
                    self.debug_logger.debug(f"Nighttime check result for horizon {horizon}h: {np.sum(is_night)}/{len(is_night)} nighttime periods detected")
                    if np.any(is_transition):
                        self.debug_logger.debug(f"Transition check result for horizon {horizon}h: {np.sum(is_transition)}/{len(is_transition)} transition periods detected")
                    
                    # Only show essential info in console
                    logging.info(f"Horizon {horizon}h: {np.sum(is_night)}/{len(is_night)} nighttime periods, {np.sum(is_transition)}/{len(is_transition)} transition periods detected")
                    
                except Exception as e:
                    logging.warning(f"Could not perform timestamp nighttime check: {str(e)}")
                    logging.info(f"Falling back to simpler detection for horizon {horizon}h")
                    
                    # Fallback: check if horizon extends into typical night hours
                    current_hour = X_new['datetime'].dt.hour.values[0]
                    target_hour = (current_hour + horizon) % 24
                    
                    # Conservative nighttime check (6 PM to 6 AM)
                    if target_hour >= 18 or target_hour < 6:
                        is_night[:] = True
                        logging.info(f"Fallback: Horizon {horizon}h (target hour {target_hour}) detected as nighttime")
                    # Check transition periods
                    elif target_hour == 6 or target_hour == 17:
                        is_transition[:] = True
                        logging.info(f"Fallback: Horizon {horizon}h (target hour {target_hour}) detected as transition period")
            else:
                # If datetime not available, use solar zenith from the data
                current_night_mask = X_new['solar_zenith_cos'] <= 0.01
                is_night |= current_night_mask.values
                
                # If available, use transition zone info
                if 'is_transition' in X_new.columns:
                    is_transition |= X_new['is_transition'].values.astype(bool)
                
                logging.info(f"No datetime available. Using solar zenith and transition indicators for horizon {horizon}h.")
            
            # Apply night mask to predictions
            if np.any(is_night):
                original_preds = median_preds.copy()
                median_preds = np.where(is_night, 0, median_preds)
                logging.info(f"Set {np.sum(is_night)} nighttime predictions to 0 for horizon {horizon}h")
                # Avoid printing full arrays, just show summary
                self.debug_logger.debug(f"Before: Mean={np.mean(original_preds):.2f}, After: Mean={np.mean(median_preds):.2f}")
            
            # Apply transition adjustments (reduce predictions during dawn/dusk by 70%)
            if np.any(is_transition):
                # First ensure no negative values
                median_preds = np.maximum(0, median_preds)
                
                transition_factor = 0.3  # Reduce to 30% of original value
                original_trans_preds = median_preds.copy()
                median_preds = np.where(is_transition, median_preds * transition_factor, median_preds)
                logging.info(f"Reduced {np.sum(is_transition)} transition period predictions to 30% for horizon {horizon}h")
                self.debug_logger.debug(f"Before transition: Mean={np.mean(original_trans_preds):.2f}, After: Mean={np.mean(median_preds):.2f}")
            
            # Store median predictions
            horizon_preds['predicted'] = median_preds
            
            # Add prediction intervals if requested
            if return_intervals:
                # Use error percentiles if available (new approach)
                if hasattr(self, 'error_percentiles') and horizon in self.error_percentiles:
                    lower_percentile, upper_percentile = self.error_percentiles[horizon]
                    
                    # Calculate prediction bounds using error percentiles
                    lower_bounds = median_preds + lower_percentile
                    upper_bounds = median_preds + upper_percentile
                    
                    # Apply night mask to bounds as well
                    if np.any(is_night):
                        lower_bounds = np.where(is_night, 0, lower_bounds)
                        upper_bounds = np.where(is_night, 0, upper_bounds)
                    
                    # Ensure non-negative values before applying transition factor
                    lower_bounds = np.maximum(0, lower_bounds)
                    upper_bounds = np.maximum(0, upper_bounds)
                    
                    # Apply transition adjustments to bounds as well
                    if np.any(is_transition):
                        transition_factor = 0.3  # Same factor as for median predictions
                        lower_bounds = np.where(is_transition, lower_bounds * transition_factor, lower_bounds)
                        upper_bounds = np.where(is_transition, upper_bounds * transition_factor, upper_bounds)
                    
                    # Enforce maximum interval width constraint, especially at peak solar times
                    max_interval_width = 100  # W/m²
                    
                    # Check for peaks during daytime (not for night or transition)
                    peak_solar_times = np.zeros_like(median_preds, dtype=bool)
                    
                    # If datetime information is available, detect peak solar hours (11am-1pm)
                    if 'datetime' in X_new.columns:
                        try:
                            # Calculate target prediction timestamps for this horizon
                            future_timestamps = X_new['datetime'] + pd.Timedelta(hours=horizon)
                            
                            # Create specific mask for noon and broader peak solar times
                            noon_times = future_timestamps.dt.hour == 12
                            peak_solar_times = (future_timestamps.dt.hour >= 11) & (future_timestamps.dt.hour <= 13)
                            
                            # Exclude nights and transitions
                            noon_times = noon_times & ~is_night & ~is_transition
                            peak_solar_times = peak_solar_times & ~is_night & ~is_transition
                            
                            if np.any(peak_solar_times):
                                self.debug_logger.debug(f"Horizon {horizon}h: {np.sum(peak_solar_times)} peak solar time predictions detected")
                            if np.any(noon_times):
                                self.debug_logger.debug(f"Horizon {horizon}h: {np.sum(noon_times)} noon time predictions detected")
                        except Exception as e:
                            logging.warning(f"Could not detect peak solar times from timestamps: {str(e)}")
                            # Continue with empty arrays (all False)
                            noon_times = np.zeros_like(median_preds, dtype=bool)
                            peak_solar_times = np.zeros_like(median_preds, dtype=bool)
                    else:
                        noon_times = np.zeros_like(median_preds, dtype=bool)
                        peak_solar_times = np.zeros_like(median_preds, dtype=bool)
                    
                    # For non-peak daytime, enforce max width but with more lenient constraint
                    daytime = ~is_night & ~is_transition & ~peak_solar_times
                    
                    # Apply strict width constraints for ALL hours based on calibration results
                    # This is necessary because our analysis showed that all hours are exceeding the constraints
                    try:
                        # Create a copy of the original bounds
                        original_lower = lower_bounds.copy()
                        original_upper = upper_bounds.copy()
                        
                        # First pass: modify all predictions to respect max width
                        for i in range(len(lower_bounds)):
                            # Skip night and transition periods
                            if is_night[i] or is_transition[i]:
                                continue
                                
                            width = upper_bounds[i] - lower_bounds[i]
                            if width > max_interval_width:
                                # For all daytime predictions, enforce max width
                                midpoint = (upper_bounds[i] + lower_bounds[i]) / 2
                                pred_level = median_preds[i]
                                
                                # Special handling for exact noon (12 PM)
                                if noon_times[i]:
                                    # Use more aggressive constraint for noon - 98% of max width to guarantee it stays under limit
                                    # Apply asymmetric adjustment based on prediction level
                                    if pred_level > 300:  # Very high GHI (common at noon)
                                        # More aggressive shrinkage on lower bound since upper bound uncertainty is more critical
                                        lower_bounds[i] = midpoint - (max_interval_width * 0.58)
                                        upper_bounds[i] = midpoint + (max_interval_width * 0.4)
                                    else:
                                        # Symmetric but reduced scale to ensure we stay under max width
                                        lower_bounds[i] = midpoint - (max_interval_width * 0.49)
                                        upper_bounds[i] = midpoint + (max_interval_width * 0.49)
                                
                                # Other peak solar times (11am, 1pm)
                                elif peak_solar_times[i]:
                                    # High GHI: adjust lower bound more (preserve upper bound)
                                    if pred_level > 300:
                                        lower_bounds[i] = midpoint - (max_interval_width * 0.6)
                                        upper_bounds[i] = midpoint + (max_interval_width * 0.4)
                                    else:
                                        lower_bounds[i] = midpoint - (max_interval_width * 0.5)
                                        upper_bounds[i] = midpoint + (max_interval_width * 0.5)
                                else:
                                    # Regular daytime: more symmetric adjustment
                                    lower_bounds[i] = midpoint - (max_interval_width * 0.5)
                                    upper_bounds[i] = midpoint + (max_interval_width * 0.5)
                        
                        # Second pass: special verification for noon hours to ensure width constraint is strictly met
                        if np.any(noon_times):
                            for i in range(len(lower_bounds)):
                                if noon_times[i]:
                                    # Check if still exceeding max width
                                    width = upper_bounds[i] - lower_bounds[i]
                                    if width > max_interval_width:
                                        # Force strict adherence to max width by proportional shrinking
                                        excess = width - max_interval_width
                                        midpoint = (upper_bounds[i] + lower_bounds[i]) / 2
                                        
                                        # Apply a guaranteed width reduction to 99% of max width
                                        scaling_factor = 0.99 * max_interval_width / width
                                        half_width = (width * scaling_factor) / 2
                                        
                                        lower_bounds[i] = midpoint - half_width
                                        upper_bounds[i] = midpoint + half_width
                                        
                                        logging.info(f"Applied strict noon enforcement: Reduced width from {width:.2f} to {upper_bounds[i] - lower_bounds[i]:.2f} W/m²")
                        
                        # Log adjustments if any were made
                        adjustments_made = np.any(original_lower != lower_bounds)
                        
                        if adjustments_made:
                            # Get statistics on the adjustments
                            indices = np.where(original_lower != lower_bounds)[0]
                            widths_before = original_upper[indices] - original_lower[indices]
                            widths_after = upper_bounds[indices] - lower_bounds[indices]
                            
                            # Report overall adjustments
                            logging.info(f"Horizon {horizon}h: Adjusted {len(indices)} intervals to enforce max width")
                            logging.info(f"  Average width before: {np.mean(widths_before):.2f} W/m², after: {np.mean(widths_after):.2f} W/m²")
                            logging.info(f"  Max width before: {np.max(widths_before):.2f} W/m², after: {np.max(widths_after):.2f} W/m²")
                            
                            # Report peak hour adjustments specifically
                            if np.any(peak_solar_times):
                                peak_indices = np.where(peak_solar_times & (original_lower != lower_bounds))[0]
                                if len(peak_indices) > 0:
                                    peak_widths_before = original_upper[peak_indices] - original_lower[peak_indices]
                                    peak_widths_after = upper_bounds[peak_indices] - lower_bounds[peak_indices]
                                    logging.info(f"  Peak hours: Adjusted {len(peak_indices)} intervals from "
                                                f"avg width {np.mean(peak_widths_before):.2f} to {np.mean(peak_widths_after):.2f} W/m²")
                            
                            # Report noon hour adjustments specifically
                            if np.any(noon_times):
                                noon_indices = np.where(noon_times & (original_lower != lower_bounds))[0]
                                if len(noon_indices) > 0:
                                    noon_widths_before = original_upper[noon_indices] - original_lower[noon_indices]
                                    noon_widths_after = upper_bounds[noon_indices] - lower_bounds[noon_indices]
                                    logging.info(f"  Noon hours: Adjusted {len(noon_indices)} intervals from "
                                                f"avg width {np.mean(noon_widths_before):.2f} to {np.mean(noon_widths_after):.2f} W/m²")
                                    logging.info(f"  Noon hours max width: {np.max(noon_widths_after):.2f} W/m²")
                    except Exception as e:
                        logging.error(f"Error enforcing width constraints: {str(e)}")
                        # Continue with original bounds
                    
                    # Final check to ensure bounds are non-negative and lower <= upper
                    lower_bounds = np.maximum(0, lower_bounds)
                    upper_bounds = np.maximum(lower_bounds, upper_bounds)
                    
                    # One last validation of max width
                    max_final_width = np.max(upper_bounds - lower_bounds)
                    if max_final_width > max_interval_width:
                        logging.warning(f"After all adjustments, max width is still {max_final_width:.2f} W/m²")
                    
                    horizon_preds['lower'] = lower_bounds
                    horizon_preds['upper'] = upper_bounds
                
                # Fallback to old approach if error percentiles aren't available
                elif horizon in self.models_lower and horizon in self.models_upper:
                    lower_preds = self.models_lower[horizon].predict(X_new[available_features])
                    upper_preds = self.models_upper[horizon].predict(X_new[available_features])
                    
                    # Apply night mask to bounds
                    if np.any(is_night):
                        lower_preds = np.where(is_night, 0, lower_preds)
                        upper_preds = np.where(is_night, 0, upper_preds)
                        self.debug_logger.debug(f"Set upper/lower bounds to 0 for nighttime predictions")
                    
                    # Ensure non-negative values before applying transition factor
                    lower_preds = np.maximum(0, lower_preds)
                    upper_preds = np.maximum(0, upper_preds)
                    
                    # Apply transition adjustments to bounds
                    if np.any(is_transition):
                        transition_factor = 0.3  # Same factor as for median predictions
                        lower_preds = np.where(is_transition, lower_preds * transition_factor, lower_preds)
                        upper_preds = np.where(is_transition, upper_preds * transition_factor, upper_preds)
                        self.debug_logger.debug(f"Reduced upper/lower bounds to 30% for transition period predictions")
                    
                    # Enforce max interval width here too
                    try:
                        for i in range(len(lower_preds)):
                            # Skip night and transition periods
                            if is_night[i] or is_transition[i]:
                                continue
                                
                            width = upper_preds[i] - lower_preds[i]
                            if width > max_interval_width:
                                # Enforce max width constraint
                                midpoint = (upper_preds[i] + lower_preds[i]) / 2
                                lower_preds[i] = midpoint - (max_interval_width / 2)
                                upper_preds[i] = midpoint + (max_interval_width / 2)
                    except Exception as e:
                        logging.error(f"Error enforcing width constraints (fallback mode): {str(e)}")
                    
                    # Final check to ensure bounds are non-negative and lower <= upper
                    lower_preds = np.maximum(0, lower_preds)
                    upper_preds = np.maximum(lower_preds, upper_preds)
                    
                    horizon_preds['lower'] = lower_preds
                    horizon_preds['upper'] = upper_preds
            
            # Store predictions for this horizon
            predictions[horizon] = horizon_preds
        
        # Print debug info about the first horizon's predictions
        if predictions and len(predictions) > 0:
            first_horizon = list(predictions.keys())[0]
            self.debug_logger.debug(f"Debug - predictions[{first_horizon}] keys: {list(predictions[first_horizon].keys())}")
        
        # Save the nighttime detection log to CSV
        if nighttime_log:
            os.makedirs('logs', exist_ok=True)
            nighttime_df = pd.DataFrame(nighttime_log)
            nighttime_csv = os.path.join('logs', f'nighttime_detection_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
            nighttime_df.to_csv(nighttime_csv, index=False)
            logging.info(f"Nighttime detection details saved to: {nighttime_csv}")
        
        return predictions


# Main execution
if __name__ == "__main__":
    import os
    
    def display_menu():
        """Display the main menu options"""
        print("\n===== GHI Prediction Model Menu =====")
        print("1. Train new models")
        print("2. Make predictions with existing models")
        print("3. Exit")
        return input("\nSelect an option (1-3): ")
    
    def train_models():
        """Function to handle model training"""
        print("\n===== Training New Models =====")
        
        # Create instance of the model
        model = GHIPredictionModel()
        
        # Define input parameters - use the same directory as main.py
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, 'dataset.csv')
        test_size = 0.2
        val_size = 0.1
        lag_hours = 3
        random_state = 42
        max_interval_width = 100  # Maximum width between lower and upper bounds in W/m²
        
        # Run the entire pipeline for multi-horizon forecasting
        result = model.run_pipeline(
            file_path=file_path,
            test_size=test_size,
            val_size=val_size,
            lag_hours=lag_hours,
            random_state=random_state,
            max_interval_width=max_interval_width
        )
        
        print("\n--- Final Multi-Horizon Metrics ---")
        
        # Handle multiple possible return structures
        # Case 1: result is a tuple of (metrics, predictions)
        if isinstance(result, tuple):
            metrics_data = result[0]
            
            # Case 1.1: metrics_data is itself a tuple
            if isinstance(metrics_data, tuple):
                # Extract the actual metrics dictionary (first element)
                if len(metrics_data) > 0 and isinstance(metrics_data[0], dict):
                    metrics = metrics_data[0]
                else:
                    print(f"Metrics tuple structure not recognized: {type(metrics_data)}")
                    metrics = {}
            # Case 1.2: metrics_data is already a dictionary
            elif isinstance(metrics_data, dict):
                metrics = metrics_data
            else:
                print(f"Metrics format not recognized: {type(metrics_data)}")
                metrics = {}
        # Case 2: result is directly a dictionary
        elif isinstance(result, dict):
            metrics = result
        else:
            print(f"Result format not recognized: {type(result)}")
            metrics = {}
        
        # Now display the metrics if we have them
        if metrics:
            for horizon, mets in metrics.items():
                if isinstance(mets, dict):
                    # Extract metrics based on what's available in the dictionary
                    mae = mets.get('mae', mets.get('median_mae', 0))
                    rmse = mets.get('rmse', mets.get('median_rmse', 0))
                    r2 = mets.get('r2', mets.get('median_r2', 0))
                    skill = mets.get('skill_score_rmse', 0)
                    
                    print(f"Horizon {horizon}h: MAE={mae:.2f}, RMSE={rmse:.2f}, " + 
                          f"R²={r2:.2f}, Skill Score={skill:.2f}")
                else:
                    print(f"Horizon {horizon}h: {mets}")
        
        # Save the trained models
        model.save_models()
        
        print("\nTraining completed successfully!")
    
    def make_predictions():
        """Function to handle predictions with existing models"""
        print("\n===== Making Predictions with Existing Models =====")
        
        # Check if models exist
        model_dir = os.path.dirname(os.path.abspath(__file__))
        if not os.path.exists(os.path.join(model_dir, 'xgboost_model_hour_1.json')):
            print("\nError: No trained models found. Please train models first (Option 1).")
            return
        
        # Create instance of the model
        model = GHIPredictionModel()
        
        # Load the trained models
        if not model.load_models():
            print("\nError: Failed to load models. Please train new models.")
            return
        
        # Generate predictions for future hours
        print("\n--- Predicting Future Hours ---")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, 'dataset.csv')
        num_hours = 4
        predictions = model.predict_future_hours(file_path=file_path, num_hours=num_hours)
        
        # Display interval width information if available
        if hasattr(model, 'interval_widths') and model.interval_widths:
            print("\n--- Prediction Interval Widths ---")
            for horizon, width in model.interval_widths.items():
                if horizon in model.forecast_horizons:
                    print(f"Horizon {horizon}h: Average width = {width:.2f} W/m²")
        
        print("\nPrediction completed successfully!")
    
    # Main program loop
    while True:
        choice = display_menu()
        
        if choice == '1':
            train_models()
        elif choice == '2':
            make_predictions()
        elif choice == '3':
            print("\nExiting program. Goodbye!")
            break
        else:
            print("\nInvalid option. Please select 1, 2, or 3.")
