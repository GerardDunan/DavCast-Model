import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.autograd import Variable
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.feature_selection import mutual_info_regression
from sklearn.metrics import r2_score
from scipy import stats
import os
import torch.nn.functional as F
from torch.utils.data import WeightedRandomSampler
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.optim.lr_scheduler import OneCycleLR  # Add this import
from torch.utils.data import Dataset, DataLoader, random_split, TensorDataset
import datetime
import warnings
import copy
import requests
from datetime import datetime
import traceback
warnings.filterwarnings('ignore')

# Create figures directory if it doesn't exist
if not os.path.exists('figures'):
    os.makedirs('figures')

# Define global variables
base_features = [
    'ramp_up_rate',        # Ramp features (3)
    'clear_sky_ratio',
    'hour_ratio',
    'prev_hour',           # History features (3)
    'rolling_mean_3h',
    'prev_day_same_hour',
    'UV Index',            # Environmental features (4)
    'Average Temperature',
    'Average Humidity',
    'clear_sky_radiation'
]  # Total 10 base features + 2 time features = 12 features

# Initialize scalers as global variables
scaler_X = StandardScaler()
scaler_y = StandardScaler()

# Constants
DAVAO_LATITUDE = 7.0707
DAVAO_LONGITUDE = 125.6087
RANDOM_SEED = 42
torch.manual_seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)

class EarlyStopping:
    def __init__(self, patience=7, min_delta=0, verbose=False):
        self.patience = patience
        self.min_delta = min_delta
        self.verbose = verbose
        self.counter = 0
        self.best_loss = None
        self.early_stop = False
        
    def __call__(self, val_loss):
        if self.best_loss is None:
            self.best_loss = val_loss
        elif val_loss > self.best_loss - self.min_delta:
            self.counter += 1
            if self.verbose:
                print(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_loss = val_loss
            self.counter = 0
        return self.early_stop

class SolarDataset(Dataset):
    def __init__(self, X, time_features, y):
        self.X = torch.FloatTensor(X)
        self.time_features = torch.FloatTensor(time_features)
        self.y = torch.FloatTensor(y)
        
    def __len__(self):
        return len(self.X)
    
    def __getitem__(self, idx):
        return self.X[idx], self.time_features[idx], self.y[idx]

class WeightedSolarDataset(Dataset):
    def __init__(self, X, y, weights):
        self.X = torch.FloatTensor(X)
        self.y = torch.FloatTensor(y)
        self.weights = torch.FloatTensor(weights)
        
    def __len__(self):
        return len(self.X)
    
    def __getitem__(self, idx):
        return self.X[idx], self.y[idx], self.weights[idx]


def preprocess_data(data_path):
    """Preprocess data with enhanced feature engineering"""
    try:
        # Load the CSV file first
        data = pd.read_csv(data_path)
        
        if 'timestamp' not in data.columns:
            data['timestamp'] = pd.to_datetime(data['Date & Time'])
        
        data['date'] = data['timestamp'].dt.date
        data['hour'] = data['timestamp'].dt.hour
        data['month'] = data['timestamp'].dt.month
        data['day_of_year'] = data['timestamp'].dt.dayofyear
        
        # Enhanced time features
        data['hour_sin'] = np.sin(2 * np.pi * data['hour']/24)
        data['hour_cos'] = np.cos(2 * np.pi * data['hour']/24)
        data['day_sin'] = np.sin(2 * np.pi * data['day_of_year']/365)
        data['day_cos'] = np.cos(2 * np.pi * data['day_of_year']/365)
        
        # Calculate clear sky radiation first
        data['clear_sky_radiation'] = data.apply(
            lambda row: calculate_clear_sky_radiation(
                row['hour'], 
                DAVAO_LATITUDE, 
                DAVAO_LONGITUDE, 
                pd.to_datetime(row['date'])
            ), 
            axis=1
        )
        
        # Improved rolling statistics with larger window
        data['rolling_mean_3h'] = data.groupby('date')['Solar Rad - W/m^2'].transform(
            lambda x: x.rolling(window=3, min_periods=1).mean()
        )
        data['rolling_mean_6h'] = data.groupby('date')['Solar Rad - W/m^2'].transform(
            lambda x: x.rolling(window=6, min_periods=1).mean()  # Fixed missing parenthesis
        )
        data['rolling_std_3h'] = data.groupby('date')['Solar Rad - W/m^2'].transform(
            lambda x: x.rolling(window=3, min_periods=1).std()
        )
        
        # Calculate cloud impact
        data['cloud_impact'] = 1 - (data['Solar Rad - W/m^2'] / data['clear_sky_radiation'].clip(lower=1))
        
        # Calculate solar trend
        data['solar_trend'] = data.groupby('date')['Solar Rad - W/m^2'].diff()
        
        # Enhanced lag features
        data['prev_hour'] = data.groupby('date')['Solar Rad - W/m^2'].shift(1)
        data['prev_2hour'] = data.groupby('date')['Solar Rad - W/m^2'].shift(2)
        data['prev_3hour'] = data.groupby('date')['Solar Rad - W/m^2'].shift(3)
        data['prev_day_same_hour'] = data.groupby('hour')['Solar Rad - W/m^2'].shift(24)
        
        # Interaction features
        data['clear_sky_ratio'] = data['Solar Rad - W/m^2'] / data['clear_sky_radiation'].clip(lower=1)
        data['humidity_impact'] = 1 - (data['Average Humidity'] / 100)
        data['temp_clear_sky_interaction'] = data['Average Temperature'] * data['clear_sky_radiation'] / 1000
        
        # Fill NaN values
        for col in data.select_dtypes(include=[np.number]).columns:
            if col != 'hour':
                if 'prev' in col or 'rolling' in col:
                    data[col] = data[col].fillna(method='ffill').fillna(method='bfill')
                else:
                    data[col] = data[col].fillna(data.groupby('hour')[col].transform('mean'))
        
        return data, data['date'].max(), None
        
    except Exception as e:
        print(f"Error in preprocess_data: {str(e)}")
        traceback.print_exc()
        return None, None, None

def analyze_features(data):
    """Analyze and select the most important features using multiple methods"""
    try:
        print("\nPerforming feature selection analysis...")
        
        # Create a copy of the data and drop non-numeric columns
        numeric_data = data.select_dtypes(include=[np.number]).copy()
        
        # Drop any datetime columns if they exist
        datetime_cols = ['date', 'timestamp']
        numeric_data = numeric_data.drop(columns=[col for col in datetime_cols if col in numeric_data.columns])
        
        # Select target variable - using ground data's solar radiation
        target = numeric_data['Solar Rad - W/m^2']
        features = numeric_data.drop(['Solar Rad - W/m^2'], axis=1)
        
        # Calculate mutual information scores
        mi_scores = mutual_info_regression(features, target)
        feature_importance = pd.DataFrame({
            'feature': features.columns,
            'importance': mi_scores
        }).sort_values('importance', ascending=False)
        
        # Calculate correlations
        correlations = features.corrwith(target).abs()
        
        # Combine both metrics
        feature_importance['correlation'] = [correlations[feat] for feat in feature_importance['feature']]
        
        # Calculate combined score
        feature_importance['combined_score'] = (
            feature_importance['importance'] * 0.6 + 
            feature_importance['correlation'] * 0.4
        )
        
        # Sort by combined score
        feature_importance = feature_importance.sort_values('combined_score', ascending=False)
        
        print("\nFeature Importance Analysis:")
        print("============================")
        print("\nAll Features Ranked by Importance:")
        print("----------------------------------")
        for idx, row in feature_importance.iterrows():
            print(f"{row['feature']:<30} | MI Score: {row['importance']:.4f} | "
                  f"Correlation: {row['correlation']:.4f} | Combined: {row['combined_score']:.4f}")
        
        # Select top features with higher threshold and remove redundant ones
        threshold = 1.4  # Increased threshold
        initial_selection = feature_importance[feature_importance['combined_score'] > threshold]
        
        # List of feature groups that are likely redundant
        redundant_groups = [
            ['Average Temperature', 'Heat Index', 'Average THW Index', 'Average Wind Chill'],  # Keep THSW Index separate
            ['cloud_impact', 'cloud_cover', 'clearness_index'],  # Keep all cloud-related features
            ['prev_hour', 'prev_2hour'],  # Keep rolling_mean_3h separate
            ['hour_sin', 'hour_cos']  # Keep hour separate
        ]
        
        # Keep only the best feature from each redundant group
        final_features = []
        used_groups = set()
        
        for feat in initial_selection['feature']:
            # Check if feature belongs to any redundant group
            in_redundant_group = False
            for group in redundant_groups:
                if feat in group:
                    group_key = tuple(group)  # Convert list to tuple for set membership
                    if group_key not in used_groups:
                        final_features.append(feat)
                        used_groups.add(group_key)
                    in_redundant_group = True
                    break
            
            # If feature is not in any redundant group, add it
            if not in_redundant_group:
                final_features.append(feat)
        
        # Always include these essential features regardless of threshold
        essential_features = [
            'UV Index', 
            'clear_sky_radiation', 
            'rolling_mean_3h',  # Added as essential
            'Average THSW Index',  # Added as essential
            'prev_hour'  # Added as essential
        ]
        for feat in essential_features:
            if feat not in final_features and feat in features.columns:
                final_features.append(feat)
        
        print("\nSelected Features After Redundancy Removal:")
        print("----------------------------------------")
        for feat in final_features:
            score = feature_importance[feature_importance['feature'] == feat]['combined_score'].iloc[0]
            print(f"{feat:<30} | Combined Score: {score:.4f}")
        
        return final_features
        
    except Exception as e:
        print(f"Error in analyze_features: {str(e)}")
        traceback.print_exc()
        return None

def prepare_features(df):
    try:
        # Calculate all features first
        df = calculate_all_features(df)
        
        # Fill NaN values
        df = df.copy()
        
        # Prepare feature matrix ensuring all features exist
        feature_matrix = []
        for feature in base_features:
            if feature not in df.columns:
                print(f"Warning: Missing feature {feature}, adding zeros")
                df[feature] = 0.0
            feature_matrix.append(df[feature].values)
        
        # Convert to numpy array
        X = np.column_stack(feature_matrix)
        
        # Add time features
        hour_sin = np.sin(2 * np.pi * df['hour'] / 24)
        hour_cos = np.cos(2 * np.pi * df['hour'] / 24)
        time_features = np.column_stack([hour_sin, hour_cos])
        
        # Combine all features
        X = np.column_stack([X, time_features])
        
        print(f"\nFeature matrix shape: {X.shape}")
        print("Features included:", base_features + ['hour_sin', 'hour_cos'])
        
        return X, df['Solar Rad - W/m^2'].values, base_features
        
    except Exception as e:
        print(f"Error in prepare_features: {str(e)}")
        traceback.print_exc()
        return None, None, None

def calculate_all_features(df):
    """Calculate all required features"""
    # Calculate clear sky radiation
    df['clear_sky_radiation'] = df.apply(
        lambda row: calculate_clear_sky_radiation(
            row['hour'], 
            DAVAO_LATITUDE, 
            DAVAO_LONGITUDE, 
            pd.to_datetime(row['date'])
        ), 
        axis=1
    )
    
    # Calculate ramp features
    df['ramp_up_rate'] = df.groupby('hour')['Solar Rad - W/m^2'].diff().rolling(7, min_periods=1).mean()
    df['clear_sky_ratio'] = df['Solar Rad - W/m^2'] / df['clear_sky_radiation'].clip(lower=1)
    df['hour_ratio'] = df['hour'] / 24.0
    
    # Calculate history features
    df['prev_hour'] = df.groupby('date')['Solar Rad - W/m^2'].shift(1)
    df['rolling_mean_3h'] = df.groupby('date')['Solar Rad - W/m^2'].transform(
        lambda x: x.rolling(window=3, min_periods=1).mean()
    )
    df['prev_day_same_hour'] = df.groupby('hour')['Solar Rad - W/m^2'].shift(1)
    
    # Calculate environmental features
    df['cloud_impact'] = 1 - (df['Solar Rad - W/m^2'] / df['clear_sky_radiation'].clip(lower=1))
    df['solar_trend'] = df.groupby('date')['Solar Rad - W/m^2'].diff()
    
    return df

class ImprovedSolarPredictor(nn.Module):
    def __init__(self, input_dim, hidden_dim=128):
        super(ImprovedSolarPredictor, self).__init__()
        
        # Feature dimensions - must match base_features length
        self.ramp_features = 3     # First 3 features
        self.history_features = 3  # Next 3 features
        self.env_features = 4      # Last 4 features
        self.time_features = 2     # Time features added separately
        
        print(f"\nNetwork Architecture:")
        print(f"Ramp features: {self.ramp_features}")
        print(f"History features: {self.history_features}")
        print(f"Environmental features: {self.env_features}")
        print(f"Time features: {self.time_features}")
        print(f"Total features: {self.ramp_features + self.history_features + self.env_features + self.time_features}")
        
        # Ramp features network
        self.ramp_net = nn.Sequential(
            nn.Linear(self.ramp_features, hidden_dim // 2),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim // 2),
            nn.Dropout(0.2)
        )
        
        # Historical features network
        self.history_net = nn.Sequential(
            nn.Linear(self.history_features, hidden_dim // 2),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim // 2),
            nn.Dropout(0.2)
        )
        
        # Environmental features network
        self.env_net = nn.Sequential(
            nn.Linear(self.env_features, hidden_dim // 2),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim // 2),
            nn.Dropout(0.2)
        )
        
        # Time features network
        self.time_net = nn.Sequential(
            nn.Linear(self.time_features, hidden_dim // 4),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim // 4)
        )
        
        # Combination network
        combined_dim = (hidden_dim // 2) * 3 + (hidden_dim // 4)  # All features combined
        self.final_net = nn.Sequential(
            nn.Linear(combined_dim, hidden_dim),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim),
            nn.Dropout(0.3),
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_dim // 2),
            nn.Dropout(0.2),
            nn.Linear(hidden_dim // 2, 1),
            nn.ReLU()  # Ensure non-negative output
        )
        
        # Initialize weights
        self.apply(self._init_weights)
        
    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            nn.init.kaiming_normal_(module.weight, mode='fan_in', nonlinearity='relu')
            if module.bias is not None:
                nn.init.constant_(module.bias, 0)
                
    def forward(self, weather_features, time_features):
        try:
            # Split weather features
            ramp = weather_features[:, :self.ramp_features]
            history = weather_features[:, self.ramp_features:self.ramp_features + self.history_features]
            env = weather_features[:, self.ramp_features + self.history_features:]
            
            # Process each feature group
            ramp_out = self.ramp_net(ramp)
            history_out = self.history_net(history)
            env_out = self.env_net(env)
            time_out = self.time_net(time_features)
            
            # Combine all features
            combined = torch.cat([ramp_out, history_out, env_out, time_out], dim=1)
            
            # Final prediction
            output = self.final_net(combined)
            
            return output
            
        except Exception as e:
            print(f"\nError in forward pass: {str(e)}")
            print(f"Input shapes:")
            print(f"Weather features: {weather_features.shape}")
            print(f"Time features: {time_features.shape}")
            traceback.print_exc()
            return None

class CustomLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.mse = nn.MSELoss(reduction='none')
        self.mae = nn.L1Loss(reduction='none')
        
    def forward(self, pred, target, time_features):
        try:
            # Convert inputs to float32 for better numerical stability
            pred = pred.float()
            target = target.float().unsqueeze(1)
            time_features = time_features.float()
            
            # Calculate hour of day
            hour = torch.atan2(time_features[:, 0], time_features[:, 1])
            hour = (hour / np.pi * 12 + 12) % 24
            
            # Base loss with epsilon to prevent division by zero
            epsilon = 1e-8
            mse_loss = self.mse(pred, target)
            mae_loss = self.mae(pred, target)
            base_loss = 0.7 * mse_loss + 0.3 * mae_loss
            
            # Clip predictions to prevent extreme values
            pred_clipped = torch.clamp(pred, min=0.0, max=1500.0)
            
            # Morning ramp-up penalties
            morning_ramp = (hour >= 6) & (hour < 10)
            morning_penalty = torch.where(
                morning_ramp.unsqueeze(1) & (pred_clipped < target),
                torch.abs(target - pred_clipped) * 8.0,
                torch.zeros_like(pred_clipped)
            )
            
            # Peak hours penalties
            peak_hours = (hour >= 10) & (hour <= 14)
            peak_penalty = torch.where(
                peak_hours.unsqueeze(1),
                torch.abs(target - pred_clipped) * 5.0,
                torch.zeros_like(pred_clipped)
            )
            
            # Night hours penalties
            night_hours = (hour < 6) | (hour >= 18)
            night_penalty = torch.where(
                night_hours.unsqueeze(1),
                pred_clipped * 10.0,
                torch.zeros_like(pred_clipped)
            )
            
            # Rapid changes penalty
            diff_penalty = torch.abs(torch.diff(pred_clipped, dim=0, prepend=pred_clipped[:1]))
            rapid_change_penalty = torch.where(
                diff_penalty > 100,
                diff_penalty * 0.1,
                torch.zeros_like(diff_penalty)
            )
            
            # Combine all penalties with gradient clipping
            total_loss = (
                torch.clamp(base_loss.mean(), max=1e6) +
                torch.clamp(morning_penalty.mean(), max=1e6) +
                torch.clamp(peak_penalty.mean(), max=1e6) +
                torch.clamp(night_penalty.mean(), max=1e6) +
                torch.clamp(rapid_change_penalty.mean(), max=1e6)
            )
            
            return total_loss
            
        except Exception as e:
            print(f"Error in loss calculation: {str(e)}")
            traceback.print_exc()
            return None

def train_model(X_train, y_train, X_test, y_test, scaler_y, epochs=200):
    try:
        input_dim = X_train.shape[1]
        print(f"Input dimension: {input_dim}")
        
        # Convert to tensors and ensure no NaN values
        X_train_tensor = torch.FloatTensor(np.nan_to_num(X_train, nan=0.0))
        y_train_tensor = torch.FloatTensor(np.nan_to_num(y_train, nan=0.0)).reshape(-1, 1)
        X_test_tensor = torch.FloatTensor(np.nan_to_num(X_test, nan=0.0))
        y_test_tensor = torch.FloatTensor(np.nan_to_num(y_test, nan=0.0)).reshape(-1, 1)
        
        # Split features
        X_train_weather = X_train_tensor[:, :-2]
        X_train_time = X_train_tensor[:, -2:]
        X_test_weather = X_test_tensor[:, :-2]
        X_test_time = X_test_tensor[:, -2:]
        
        # Create datasets
        train_dataset = TensorDataset(X_train_weather, X_train_time, y_train_tensor)
        test_dataset = TensorDataset(X_test_weather, X_test_time, y_test_tensor)
        
        # Create data loaders
        train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True)
        val_loader = DataLoader(test_dataset, batch_size=32, shuffle=False)
        
        # Initialize model
        model = ImprovedSolarPredictor(input_dim)
        criterion = nn.MSELoss()
        
        # Use AdamW optimizer with weight decay
        optimizer = optim.AdamW(
            model.parameters(),
            lr=0.001,
            weight_decay=0.01,
            eps=1e-8
        )
        
        # Learning rate scheduler
        scheduler = lr_scheduler.OneCycleLR(
            optimizer,
            max_lr=0.005,
            epochs=epochs,
            steps_per_epoch=len(train_loader),
            div_factor=10,
            final_div_factor=100
        )
        
        early_stopping = EarlyStopping(patience=20)
        best_model = None
        best_val_loss = float('inf')
        
        print("\nTraining Progress:")
        print("Epoch | Train Loss | Val Loss | Correlation | R² Score")
        print("-" * 60)
        
        for epoch in range(epochs):
            # Training phase
            model.train()
            train_loss = 0.0
            train_batches = 0
            
            for batch_weather, batch_time, batch_y in train_loader:
                optimizer.zero_grad()
                
                # Forward pass
                outputs = model(batch_weather, batch_time)
                if outputs is None:
                    continue
                    
                loss = criterion(outputs, batch_y)
                
                # Backward pass
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
                scheduler.step()
                
                train_loss += loss.item()
                train_batches += 1
            
            avg_train_loss = train_loss / train_batches if train_batches > 0 else float('inf')
            
            # Validation phase
            model.eval()
            val_loss = 0.0
            val_batches = 0
            all_preds = []
            all_targets = []
            
            with torch.no_grad():
                for batch_weather, batch_time, batch_y in val_loader:
                    outputs = model(batch_weather, batch_time)
                    if outputs is None:
                        continue
                        
                    val_loss += criterion(outputs, batch_y).item()
                    val_batches += 1
                    
                    # Store predictions and targets
                    all_preds.extend(outputs.cpu().numpy().flatten())
                    all_targets.extend(batch_y.cpu().numpy().flatten())
            
            # Calculate metrics
            if val_batches > 0:
                avg_val_loss = val_loss / val_batches
                if len(all_preds) > 0 and len(all_targets) > 0:
                    correlation = np.corrcoef(all_preds, all_targets)[0, 1]
                    r2 = r2_score(all_targets, all_preds)
                    
                    print(f"{epoch+1:3d} | {avg_train_loss:9.4f} | {avg_val_loss:8.4f} | "
                          f"{correlation:10.4f} | {r2:8.4f}")
                    
                    if avg_val_loss < best_val_loss:
                        best_val_loss = avg_val_loss
                        best_model = copy.deepcopy(model)
                        print("Best model updated!")
                    
                    if early_stopping(avg_val_loss):
                        print(f"Early stopping triggered at epoch {epoch+1}")
                        break
            
        return best_model, None, [], [], epoch + 1
        
    except Exception as e:
        print(f"Error in train_model: {str(e)}")
        traceback.print_exc()
        return None, None, [], [], 0

def predict_hourly_radiation(model, features, scaler_X, scaler_y, date, base_features):
    """Make predictions for all hours in a day"""
    predictions = []
    timestamps = []
    
    for hour in range(24):
        # Create timestamp
        timestamp = pd.Timestamp.combine(date, pd.Timestamp(f"{hour:02d}:00").time())
        
        # Make prediction for this hour
        prediction = predict_for_hour(model, hour, features, scaler_X, scaler_y, base_features)
        
        # Validate prediction
        prediction = validate_predictions(prediction, hour)
        
        predictions.append(prediction)
        timestamps.append(timestamp)
    
    # Create DataFrame with predictions
    results_df = pd.DataFrame({
        'Timestamp': timestamps,
        'Hour': [t.hour for t in timestamps],
        'Predicted Solar Radiation (W/m²)': predictions
    })
    
    # Save predictions to CSV
    results_df.to_csv('figures/hourly_predictions.csv', index=False)
    
    # Create prediction plot
    plt.figure(figsize=(12, 6))
    plt.plot(results_df['Hour'], results_df['Predicted Solar Radiation (W/m²)'], 
             marker='o', linestyle='-', linewidth=2)
    plt.title(f'Predicted Solar Radiation for {date.strftime("%Y-%m-%d")}')
    plt.xlabel('Hour of Day')
    plt.ylabel('Solar Radiation (W/m²)')
    plt.grid(True)
    plt.xticks(range(24))
    plt.ylim(bottom=0)
    plt.tight_layout()
    plt.savefig('figures/hourly_predictions.png')
    plt.close()
    
    return results_df

def analyze_feature_distributions(data):
    """Analyze feature distributions"""
    numerical_cols = data.select_dtypes(include=['float64', 'int64']).columns
    
    n_cols = 3
    n_rows = (len(numerical_cols) + n_cols - 1) // n_cols
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(18, 5*n_rows))
    axes = axes.flatten()
    
    for idx, col in enumerate(numerical_cols):
        if col != 'hour':
            sns.histplot(data=data[col], kde=True, ax=axes[idx])
            axes[idx].set_title(f'Distribution of {col}', pad=20)
            
            # Rotate x-axis labels for better readability
            axes[idx].tick_params(axis='x', rotation=45)
            
            skewness = stats.skew(data[col].dropna())
            kurtosis = stats.kurtosis(data[col].dropna())
            # Moved text box to upper right
            axes[idx].text(0.95, 0.95, 
                         f'Skewness: {skewness:.2f}\nKurtosis: {kurtosis:.2f}', 
                         transform=axes[idx].transAxes,
                         bbox=dict(facecolor='white', alpha=0.8),
                         verticalalignment='top',
                         horizontalalignment='right')
    
    for idx in range(len(numerical_cols), len(axes)):
        fig.delaxes(axes[idx])
    
    plt.tight_layout(h_pad=1.0, w_pad=0.5)
    plt.savefig('figures/feature_distributions.png')
    plt.close()

def predict_for_hour(model, hour, features, scaler_X, scaler_y, base_features):
    """Make prediction for a specific hour"""
    # Create feature vector
    feature_vector = []
    
    # Add base features
    for feature in base_features:
        feature_vector.append(features[feature][hour])
    
    # Add engineered features
    hour_sin = np.sin(2 * np.pi * hour/24)
    hour_cos = np.cos(2 * np.pi * hour/24)
    uv_squared = features['UV Index'][hour] ** 2
    uv_temp_interaction = features['UV Index'][hour] * features['Average Temperature'][hour]
    humidity_temp_interaction = features['Average Humidity'][hour] * features['Average Temperature'][hour]
    
    feature_vector.extend([
        hour_sin,
        hour_cos,
        uv_squared,
        uv_temp_interaction,
        humidity_temp_interaction
    ])
    
    # Convert to numpy array and reshape
    X = np.array([feature_vector])
    
    # Scale features
    X_scaled = scaler_X.transform(X)
    
    # Convert to tensor and reshape for RNN
    X_tensor = torch.FloatTensor(X_scaled)
    
    # Make prediction
    model.eval()
    with torch.no_grad():
        prediction = model(X_tensor, torch.FloatTensor([[hour_sin, hour_cos]]))
        prediction = scaler_y.inverse_transform(prediction.numpy().reshape(-1, 1))
    
    return prediction[0][0]

def feature_selection(data):
    """Select most important features based on mutual information scores"""
    features = data.drop(['hour', 'Solar Rad - W/m^2'], axis=1)
    target = data['Solar Rad - W/m^2']  # Direct solar radiation
    
    mi_scores = mutual_info_regression(features, target)
    important_features = pd.DataFrame({
        'feature': features.columns,
        'importance': mi_scores
    }).sort_values('importance', ascending=False)
    
    # Select top features based on importance threshold
    threshold = 0.3  # Adjust based on mutual information scores
    selected_features = important_features[important_features['importance'] > threshold]['feature'].tolist()
    
    return selected_features

class CloudAwareRNN(nn.Module):
    def __init__(self, input_dim, hidden_dim, layer_dim, output_dim):
        super(CloudAwareRNN, self).__init__()
        
        # Cloud detection branch
        self.cloud_branch = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(hidden_dim, hidden_dim//2),
            nn.BatchNorm1d(hidden_dim//2)
        )
        
        # Main radiation prediction branch
        self.radiation_branch = nn.LSTM(
            input_dim, 
            hidden_dim,  
            layer_dim, 
            batch_first=True,
            bidirectional=True
        )
        
        # Attention mechanism for cloud impact
        self.attention = nn.MultiheadAttention(hidden_dim, num_heads=4)
        
        # Combined output layers
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(hidden_dim, output_dim)
        )
    def forward(self, x):
        # Cloud feature processing
        cloud_features = self.cloud_branch(x)
        
        # Main radiation prediction
        radiation_out, _ = self.radiation_branch(x)
        
        # Apply attention mechanism
        attn_out, _ = self.attention(radiation_out, radiation_out, radiation_out)
        
        # Combine features
        combined = torch.cat((attn_out[:, -1, :], cloud_features), dim=1)
        
        # Final prediction
        out = self.fc(combined)
        return out

class CloudAwareLoss(nn.Module):
    def __init__(self, cloud_weight=0.3):
        super(CloudAwareLoss, self).__init__()
        self.cloud_weight = cloud_weight
        self.mse = nn.MSELoss()
        
    def forward(self, pred, target, cloud_features):
        # Base MSE loss
        base_loss = self.mse(pred, target)
        
        # Additional loss for sudden changes
        sudden_change_mask = cloud_features['sudden_drop'] | cloud_features['sudden_increase']
        if sudden_change_mask.any():
            cloud_loss = self.mse(
                pred[sudden_change_mask],
                target[sudden_change_mask]
            )
            return base_loss + self.cloud_weight * cloud_loss
        
        return base_loss

def train_cloud_aware_model(model, train_loader, val_loader, epochs=600):
    """Training with cloud awareness"""
    criterion = CloudAwareLoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    scheduler = OneCycleLR(optimizer, max_lr=0.01, epochs=epochs, 
                          steps_per_epoch=len(train_loader))
    
    cloud_features = CloudFeatures()
    
    for epoch in range(epochs):
        model.train()
        for batch_X, batch_y in train_loader:
            # Calculate cloud features for batch
            cloud_feat = cloud_features.calculate_cloud_features(batch_X)
            
            optimizer.zero_grad()
            output = model(batch_X)
            loss = criterion(output, batch_y, cloud_feat)
            
            loss.backward()
            optimizer.step()
            scheduler.step()
            
    return model

def predict_with_cloud_awareness(model, features, hour):
    """Make predictions with cloud awareness"""
    cloud_features = CloudFeatures()
    cloud_feat = cloud_features.calculate_cloud_features(features)
    
    model.eval()
    with torch.no_grad():
        prediction = model(features)
        
        # Adjust prediction based on cloud features
        if cloud_feat['sudden_drop'].item() > 0:
            prediction *= 0.7  # Reduce prediction for sudden drops
        elif cloud_feat['sudden_increase'].item() > 0:
            prediction *=1.3  # Increase prediction for sudden clearings
            
        # Ensure physical constraints
        prediction = torch.clamp(prediction, min=0, max=1200)
        
    return prediction.item()

def post_process_predictions(predictions, actual_values=None, cloud_cover=None):
    processed = predictions.copy()
    
    # Apply cloud cover adjustment if available
    if cloud_cover is not None:
        cloud_factor = 1.0 - (cloud_cover * 0.01)  # Convert percentage to factor
        processed *= cloud_factor
    
    # Physical constraints
    processed = np.clip(processed, 0, 1200)  # Max realistic solar radiation
    
    # Time-based corrections
    hour = np.arange(len(processed)) % 24
    night_hours = (hour < 6) | (hour > 18)
    processed[night_hours] = 0
    
    # Smooth extreme changes
    for i in range(1, len(processed)):
        max_change = 150  # Max allowed change between hours
        if abs(processed[i] - processed[i-1]) > max_change:
            direction = np.sign(processed[i] - processed[i-1])
            processed[i] = processed[i-1] + direction * max_change
    
    # Adjust based on clear sky model
    clear_sky = calculate_clear_sky_radiation(
        hour, 
        DAVAO_LATITUDE,  # Use constants instead of undefined variables
        DAVAO_LONGITUDE,
        pd.Timestamp.now().date()  # Use current date if not provided
    )
    processed = np.minimum(processed, clear_sky * 1.1)  # Allow 10% above clear sky
    
    # Ensemble with moving average for stability
    window = 3
    ma = np.convolve(processed, np.ones(window)/window, mode='same')
    processed = 0.7 * processed + 0.3 * ma
    
    return processed

def calculate_confidence_intervals(model, X_test, n_samples=100):
    """Calculate prediction confidence intervals using Monte Carlo Dropout"""
    model.train()  # Enable dropout
    predictions = []
    
    with torch.no_grad():
        for _ in range(n_samples):
            pred = model(X_test)
            predictions.append(pred.numpy())
    
    predictions = np.array(predictions)
    mean_pred = np.mean(predictions, axis=0)
    std_pred = np.std(predictions, axis=0)
    
    confidence_95 = 1.96 * std_pred
    
    return mean_pred, confidence_95

def predict_with_correction(model, X, hour, prev_value):
    """Make prediction with time-based corrections"""
    try:
        # Split features
        weather_features = torch.FloatTensor(X[:, :-2])
        time_features = torch.FloatTensor(X[:, -2:])
        
        # Get base prediction
        model.eval()
        with torch.no_grad():
            prediction = model(weather_features, time_features)
            prediction = prediction.numpy()[0][0]
        
        # Print debug information
        print(f"\nDebug Information:")
        print(f"Base prediction: {prediction:.2f}")
        print(f"Previous value: {prev_value:.2f}")
        print(f"Hour: {hour}")
        
        # Validate prediction
        prediction = validate_predictions(prediction, hour)
        
        return prediction
            
    except Exception as e:
        print(f"Error in predict_with_correction: {str(e)}")
        return None

def add_peak_features(df):
    """Add features specifically for peak radiation prediction"""
    
    # Calculate clear sky index for peak hours
    df['peak_hour'] = (df['hour'] >= 10) & (df['hour'] <= 14)
    df['clear_sky_ratio'] = df['Solar Rad - W/m^2'] / df['clear_sky_radiation'].clip(lower=1)
    
    # Add features for peak radiation periods
    df['peak_temp_ratio'] = df['Average Temperature'] / df['Average Temperature'].rolling(24).max()
    df['peak_humidity_impact'] = 1 - (df['Average Humidity'] / 100)
    
    # Add interaction terms for peak hours
    df.loc[df['peak_hour'], 'peak_features'] = (    
        df.loc[df['peak_hour'], 'peak_temp_ratio'] * 
        df.loc[df['peak_hour'], 'peak_humidity_impact'] * 
        df.loc[df['peak_hour'], 'clear_sky_ratio']
    )
    
    return df

def residual_based_correction(model, X_train, y_train):
    """Create correction factors based on residual patterns"""
    
    # Get base predictions
    model.eval()
    with torch.no_grad():
        base_pred = model(X_train).numpy()
    
    # Calculate residuals
    residuals = y_train.numpy() - base_pred
    
    # Create correction bins
    bins = np.linspace(0, 1000, 20)  # 20 bins from 0 to 1000 W/m²
    corrections = {}
    
    # Calculate mean correction for each bin
    for i in range(len(bins)-1):
        mask = (base_pred >= bins[i]) & (base_pred < bins[i+1])
        if mask.any():
            corrections[i] = np.mean(residuals[mask])
    
    return corrections, bins

def apply_residual_correction(predictions, corrections, bins):
    """Apply correction factors to predictions"""
    corrected = predictions.copy()
    
    for i in range(len(bins)-1):
        mask = (predictions >= bins[i]) & (predictions < bins[i+1])
        if mask.any() and i in corrections:
            corrected[mask] += corrections[i]
    
    return corrected

class HeteroscedasticLoss(nn.Module):
    def __init__(self):
        super(HeteroscedasticLoss, self).__init__()
        
    def forward(self, pred, target):
        # Estimate variance based on prediction magnitude
        predicted_variance = 0.1 + 0.9 * torch.sigmoid(pred/500)
        
        # Calculate weighted loss
        squared_error = torch.pow(pred - target, 2)
        loss = (squared_error / predicted_variance) + torch.log(predicted_variance)
        
        return loss.mean()

def train_with_residual_awareness(model, train_loader, val_loader, epochs=600):
    """Training loop with residual-aware components"""
    
    criterion = HeteroscedasticLoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    
    # Initialize correction factors
    corrections = None
    bins = None
    update_interval = 50
    
    # Get training data from loader
    X_train_data = []
    y_train_data = []
    for batch_X, batch_y in train_loader:
        X_train_data.append(batch_X)
        y_train_data.append(batch_y)
    X_train = torch.cat(X_train_data)
    y_train = torch.cat(y_train_data)
    
    for epoch in range(epochs):
        model.train()
        for batch_X, batch_y in train_loader:
            optimizer.zero_grad()
            
            # Forward pass
            outputs = model(batch_X)
            
            # Apply current corrections if available
            if corrections is not None:
                outputs = apply_residual_correction(outputs, corrections, bins)
            
            # Calculate loss
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()
        
        # Update correction factors periodically
        if epoch % update_interval == 0:
            corrections, bins = residual_based_correction(model, X_train, y_train)
    
    return model, corrections, bins

def predict_with_uncertainty(model, X, corrections=None, bins=None):
    """Make predictions with uncertainty estimates"""
    
    model.eval()
    with torch.no_grad():
        # Base prediction
        pred = model(X)
        
        # Apply residual correction
        if corrections is not None and bins is not None:
            pred = apply_residual_correction(pred, corrections, bins)
        
        # Estimate uncertainty
        uncertainty = 0.1 + 0.9 * torch.sigmoid(pred/500)
        
        return pred, uncertainty

def validate_data(df):
    """Validate input data"""
    required_columns = [
        'Date & Time', 'Average Barometer', 'Average Temperature',
        'Average Humidity', 'Average Dew Point', 'Average Wet Bulb',
        'Avg Wind Speed - km/h', 'Average Wind Chill', 'Heat Index',
        'Average THW Index', 'Average THSW Index', 'UV Index',
        'Solar Rad - W/m^2',
    ]
    
    missing_cols = [col for col in required_columns if col not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")
    
    # Check for invalid values
    if (df['Solar Rad - W/m^2'] < 0).any():
        raise ValueError("Negative solar radiation values found")
    
    return True

def validate_predictions(predictions, hour):
    """Enhanced validation of predictions based on time of day"""
    try:
        # Night hours (0-5, 18-23) should be zero
        if hour < 6 or hour >= 18:
            return 0.0
        
        # Maximum theoretical clear sky radiation for Davao
        max_radiation = calculate_clear_sky_radiation(
            hour, DAVAO_LATITUDE, DAVAO_LONGITUDE, 
            datetime.now().date()
        )
        
        # Add 10% margin to maximum theoretical value
        max_allowed = max_radiation * 1.1
        
        # Ensure predictions are within physical limits
        predictions = np.clip(predictions, 0, max_allowed)
        
        return float(predictions)
        
    except Exception as e:
        print(f"Error in validate_predictions: {str(e)}")
        return 0.0

def detect_weather_pattern(df):
    """Detect weather patterns that might affect predictions"""
    patterns = {
        'rainy': (df['Average Humidity'] > 85) & (df['Average Temperature'] < 25),
        'clear': (df['Average Humidity'] < 70) & (df['UV Index'] > 8),
        'cloudy': (df['Average Humidity'] > 75) & (df['UV Index'] < 5)
    }
    
    return patterns

def prepare_sequence(X, sequence_length=1):
    """Prepare sequential data for LSTM"""
    sequences = []
    for i in range(len(X) - sequence_length + 1):
        sequences.append(X[i:i + sequence_length])
    return np.array(sequences)

def extract_minute_data(data_path):
    """Extract 5-minute interval data from raw dataset"""
    try:
        # Read the CSV file
        df = pd.read_csv(data_path)
        
        # Print columns for debugging
        print("\nAvailable columns in dataset:")
        print(df.columns.tolist())
        
        # Convert timestamp and validate
        df['timestamp'] = pd.to_datetime(df['Date & Time'], format='%m/%d/%Y %H:%M')
        
        # Print data range for validation
        print("\nData range in dataset:")
        print(f"Start time: {df['timestamp'].min()}")
        print(f"End time: {df['timestamp'].max()}")
        
        if df.empty:
            raise ValueError("Dataset is empty")
            
        # Extract components
        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour
        df['minute'] = df['timestamp'].dt.minute
        
        return df
        
    except Exception as e:
        print(f"Error in extract_minute_data: {str(e)}")
        return None

def save_results(results_df, figure_path, csv_path):
    """Save results with improved plotting and data handling"""
    try:
        # Ensure figures directory exists
        if not os.path.exists('figures'):
            os.makedirs('figures')
        
        # Create the plot
        plt.figure(figsize=(12, 6))
        
        # Plot actual values with better formatting
        plt.plot(results_df['Hour'], results_df['Actual Values'], 
                marker='o', linestyle='-', linewidth=2, 
                label='Actual Values', color='blue')
        
        # Plot next hour prediction with larger marker
        next_hour_mask = results_df['Next Hour Prediction'].notna()
        if next_hour_mask.any():
            plt.plot(results_df.loc[next_hour_mask, 'Hour'], 
                    results_df.loc[next_hour_mask, 'Next Hour Prediction'],
                    marker='*', markersize=20, color='red', linestyle='none',
                    label='Next Hour Prediction')
        
        plt.title('Solar Radiation Predictions vs Actual Values')
        plt.xlabel('Hour of Day')
        plt.ylabel('Solar Radiation (W/m²)')
        plt.grid(True)
        plt.xticks(range(24))
        plt.ylim(bottom=0, top=1200)
        plt.legend()
        plt.tight_layout()
        
        # Save plot
        plt.savefig(figure_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        # Save CSV
        results_df.to_csv(csv_path, index=False, float_format='%.2f')
        
        print(f"Successfully saved results to {csv_path}")
        print(f"Successfully saved plot to {figure_path}")
            
    except Exception as e:
        print(f"Error saving results: {str(e)}")
        traceback.print_exc()

def analyze_recent_patterns(minute_data, last_timestamp, lookback_minutes=30):
    """Enhanced pattern detection focusing on rapid changes"""
    try:
        # Get recent data with longer lookback for trend analysis
        start_time = last_timestamp - pd.Timedelta(minutes=lookback_minutes)
        recent_data = minute_data[
            (minute_data['timestamp'] >= start_time) & 
            (minute_data['timestamp'] <= last_timestamp)
        ].copy()
        
        if recent_data.empty:
            print("Warning: No recent data available for pattern analysis")
            return None
            
        # Sort and calculate rolling means for smoother analysis
        recent_data = recent_data.sort_values('timestamp')
        recent_data['radiation_rolling'] = recent_data['Solar Rad - W/m^2'].rolling(3, min_periods=1).mean()
        
        # Initialize patterns with default values
        patterns = {
            'rapid_drop': False,
            'cloud_cover': False,
            'high_variability': False,
            'trend': 'stable',
            'trend_magnitude': 0.0,
            'recent_radiation': float(recent_data['Solar Rad - W/m^2'].iloc[-1]),
            'radiation_slope': 0.0,
            'likely_rain': False
        }
        
        # Weather indicators with more sensitive thresholds
        humidity_rising = (recent_data['Average Humidity'].diff().mean() > 0.2)  # Even more sensitive
        high_humidity = (recent_data['Average Humidity'].mean() > 75)  # Lower threshold
        pressure_dropping = (recent_data['Average Barometer'].diff().mean() < -0.005)  # More sensitive
        temp_dropping = (recent_data['Average Temperature'].diff().mean() < -0.05)  # More sensitive
        
        # Additional indicators
        dew_point_close = (recent_data['Average Temperature'] - recent_data['Average Dew Point']).mean() < 4
        wind_speed_rising = (recent_data['Avg Wind Speed - km/h'].diff().mean() > 0.3)
        pressure_trend = recent_data['Average Barometer'].diff().rolling(3, min_periods=1).mean().iloc[-1]
        
        # Calculate radiation metrics safely
        radiation_values = recent_data['radiation_rolling'].fillna(method='ffill').values
        raw_radiation = recent_data['Solar Rad - W/m^2'].fillna(method='ffill').values
        
        if len(radiation_values) > 2:
            # Calculate changes
            radiation_change = float(radiation_values[-1] - radiation_values[0])
            patterns['trend_magnitude'] = radiation_change
            
            # Short-term change calculation
            recent_values = raw_radiation[-min(3, len(raw_radiation)):]
            if len(recent_values) > 1 and recent_values[0] != 0:
                short_term_change = (recent_values[-1] - recent_values[0]) / recent_values[0]
            else:
                short_term_change = 0.0
            
            # Variability calculation
            recent_window = radiation_values[-min(5, len(radiation_values)):]
            radiation_variability = np.std(recent_window) / (np.mean(recent_window) + 1)
            patterns['high_variability'] = radiation_variability > 0.12  # More sensitive
            
            # Enhanced cloud detection conditions
            cloud_indicators = [
                high_humidity and temp_dropping,
                humidity_rising and pressure_dropping,
                dew_point_close and humidity_rising,
                wind_speed_rising and humidity_rising,
                radiation_variability > 0.12,
                pressure_trend < -0.01,  # Significant pressure drop
                short_term_change < -0.1  # 10% drop in short term
            ]
            
            patterns['cloud_cover'] = any(cloud_indicators)
            
            # Enhanced rain prediction with more conditions
            rain_indicators = [
                patterns['cloud_cover'] and high_humidity and pressure_dropping,
                patterns['cloud_cover'] and dew_point_close,
                high_humidity and temp_dropping and pressure_dropping,
                dew_point_close and pressure_dropping and humidity_rising,
                pressure_trend < -0.02 and humidity_rising  # Sharp pressure drop with rising humidity
            ]
            
            patterns['likely_rain'] = any(rain_indicators)
            
            # Trend detection with more sensitivity
            if radiation_change < -20 or short_term_change < -0.1:  # More sensitive thresholds
                patterns['trend'] = 'decreasing'
                if radiation_change < -50 or short_term_change < -0.15:
                    patterns['rapid_drop'] = True
            elif radiation_change > 20:
                patterns['trend'] = 'increasing'
            
            # Calculate slope
            time_intervals = np.arange(len(radiation_values))
            slope, _ = np.polyfit(time_intervals, radiation_values, 1)
            patterns['radiation_slope'] = float(slope)
            
            # Add these new indicators
            # 1. UV Index stability
            uv_stability = recent_data['UV Index'].diff().abs()
            uv_unstable = uv_stability.mean() > 0.5
            
            # 2. Short-interval radiation changes (5-minute windows)
            radiation_5min_changes = recent_data['Solar Rad - W/m^2'].diff(periods=1)
            rapid_fluctuation = radiation_5min_changes.abs().max() > 100
            
            # 3. Pressure acceleration (rate of pressure change)
            pressure_acceleration = recent_data['Average Barometer'].diff().diff()
            accelerating_pressure_drop = pressure_acceleration.mean() < -0.001
            
            # Enhanced cloud detection conditions
            cloud_indicators = [
                # ... existing indicators ...
                uv_unstable,
                rapid_fluctuation,
                accelerating_pressure_drop,
                # New specific condition for sharp drops
                any(radiation_5min_changes < -200),  # Detect 200+ W/m² drops
                pressure_trend < -0.1 and uv_unstable  # Combine pressure and UV instability
            ]
            
            # Print additional diagnostics
            print("\nShort-term indicators:")
            print(f"UV stability (diff mean): {uv_stability.mean():.3f}")
            print(f"Max 5-min radiation change: {radiation_5min_changes.min():.1f} W/m²")
            print(f"Pressure acceleration: {pressure_acceleration.mean():.4f}")
            
            # ... rest of the code ...
            
        return patterns
        
    except Exception as e:
        print(f"Error in analyze_recent_patterns: {str(e)}")
        return None

def adjust_prediction(pred, similar_cases_data, current_patterns):
    """Adjust prediction with enhanced consideration of historical low values"""
    if not similar_cases_data or current_patterns is None:
        return pred
        
    similar_cases, pattern_weights = similar_cases_data
    adjusted_pred = pred
    
    # Get the recent radiation value and patterns
    recent_radiation = current_patterns.get('recent_radiation', pred)
    
    # Check for low radiation cases in history
    if pattern_weights['low_radiation_cases']:
        low_rad_avg = np.mean(pattern_weights['low_radiation_cases'])
        low_rad_count = len(pattern_weights['low_radiation_cases'])
        
        # If we have multiple low radiation cases, be more conservative
        if low_rad_count >= 2:
            print(f"Found {low_rad_count} historical cases with low radiation")
            # Use weighted average favoring lower values
            adjusted_pred = (low_rad_avg * 0.6 + adjusted_pred * 0.4)
            print(f"Adjusting prediction based on historical low values: {adjusted_pred:.2f} W/m²")
    
    # Apply existing adjustments
    if current_patterns['likely_rain']:
        adjusted_pred = min(adjusted_pred * 0.3, recent_radiation * 0.5)
        print("Rain likely - applying severe reduction")
    elif current_patterns['cloud_cover']:
        if current_patterns['high_variability']:
            adjusted_pred = min(adjusted_pred * 0.5, recent_radiation * 0.7)
            print("Heavy cloud cover detected - applying significant reduction")
        else:
            adjusted_pred = min(adjusted_pred * 0.7, recent_radiation * 0.9)
            print("Moderate cloud cover detected - applying moderate reduction")
    
    # Consider similar historical cases with pressure drops
    pressure_cases = [x for x in pattern_weights['similar_pressure_cases'] if x is not None]
    if pressure_cases:
        pressure_avg = np.mean(pressure_cases)
        if pressure_avg < adjusted_pred * 0.8:  # If historical cases show lower values
            adjusted_pred = (pressure_avg * 0.7 + adjusted_pred * 0.3)
            print(f"Adjusting for historical pressure patterns: {adjusted_pred:.2f} W/m²")
    
    # Ensure prediction is physically reasonable
    adjusted_pred = max(0, min(adjusted_pred, 1200))
    
    print("\nPrediction adjustment details:")
    print(f"Original prediction: {pred:.2f} W/m²")
    print(f"Recent radiation: {recent_radiation:.2f} W/m²")
    print(f"Final adjusted prediction: {adjusted_pred:.2f} W/m²")
    
    return adjusted_pred

def find_similar_historical_patterns(data, target_hour, target_date, lookback_days=30):
    try:
        start_date = target_date - pd.Timedelta(days=lookback_days)
        
        # Get historical data for the same hour and surrounding hours
        historical_data = data[
            (data['timestamp'].dt.date >= start_date) & 
            (data['timestamp'].dt.date < target_date)
        ].copy()
        
        # Get current conditions
        current_conditions = data[data['timestamp'].dt.date == target_date]
        if current_conditions.empty or historical_data.empty:
            return None
        
        # Get current weather parameters
        current_hour = target_hour
        current_weather = current_conditions[
            current_conditions['hour'] == current_hour - 1
        ].iloc[0]
        
        similar_days = []
        
        # Enhanced similarity calculations
        for date in historical_data['timestamp'].dt.date.unique():
            day_data = historical_data[historical_data['timestamp'].dt.date == date]
            
            # Skip if insufficient data
            if len(day_data) < 24:
                continue
            
            target_hour_data = day_data[day_data['hour'] == target_hour]
            if target_hour_data.empty:
                continue
            
            # Get previous hours' pattern
            morning_data = day_data[
                (day_data['hour'] >= 6) & 
                (day_data['hour'] <= target_hour)]
            
            if len(morning_data) >= 2:
                ramp_rate = np.diff(morning_data['Solar Rad - W/m^2'].values).mean()
            else:
                continue
            
            # Compare weather conditions with enhanced weighting
            weather_at_hour = day_data[day_data['hour'] == current_hour - 1].iloc[0]
            
            # Weather similarity with more emphasis on key indicators
            weather_similarity = (
                (1 - abs(current_weather['Average Temperature'] - weather_at_hour['Average Temperature']) / 50) * 0.2 +
                (1 - abs(current_weather['Average Humidity'] - weather_at_hour['Average Humidity']) / 100) * 0.3 +
                (1 - abs(current_weather['UV Index'] - weather_at_hour['UV Index']) / 20) * 0.3 +
                (1 - abs(current_weather['cloud_impact'] - weather_at_hour['cloud_impact'])) * 0.2
            )
            
            # Enhanced clear sky ratio comparison
            current_clear_sky = max(1, current_weather.get('clear_sky_radiation', 1))
            historical_clear_sky = max(1, weather_at_hour.get('clear_sky_radiation', 1))
            
            current_ratio = current_weather['Solar Rad - W/m^2'] / current_clear_sky
            historical_ratio = weather_at_hour['Solar Rad - W/m^2'] / historical_clear_sky
            
            clear_sky_similarity = 1 - min(1, abs(current_ratio - historical_ratio))
            
            # Enhanced ramp rate comparison
            current_ramp = current_conditions[
                (current_conditions['hour'] >= 6) & 
                (current_conditions['hour'] <= current_hour)
            ]['Solar Rad - W/m^2'].diff().mean()
            
            if pd.isna(current_ramp):
                current_ramp = 0
            
            ramp_similarity = 1 - min(1, abs(current_ramp - ramp_rate) / 100)
            
            # Value-based similarity (new)
            value_diff = abs(target_hour_data['Solar Rad - W/m^2'].iloc[0] - current_weather['Solar Rad - W/m^2'])
            value_similarity = 1 - min(1, value_diff / 1000)
            
            # Calculate overall similarity with adjusted weights
            if 10 <= target_hour <= 14:  # Peak hours
                total_similarity = (
                    weather_similarity * 0.3 +
                    clear_sky_similarity * 0.3 +
                    ramp_similarity * 0.2 +
                    value_similarity * 0.2  # Added value similarity
                )
            else:
                total_similarity = (
                    weather_similarity * 0.4 +
                    clear_sky_similarity * 0.3 +
                    ramp_similarity * 0.3
                )
            
            # Get the actual value
            actual_value = target_hour_data['Solar Rad - W/m^2'].iloc[0]
            
            similar_days.append({
                'date': date,
                'similarity': total_similarity,
                'value': actual_value,
                'weather_similarity': weather_similarity,
                'clear_sky_similarity': clear_sky_similarity,
                'ramp_similarity': ramp_similarity,
                'value_similarity': value_similarity,  # Added
                'ramp_rate': ramp_rate
            })
        
        # Sort by similarity but prioritize higher values during peak hours
        if 10 <= target_hour <= 14:
            similar_days.sort(key=lambda x: (x['similarity'] * 0.7 + (x['value'] / 1000) * 0.3), reverse=True)
        else:
            similar_days.sort(key=lambda x: x['similarity'], reverse=True)
        
        # Print analysis
        print("\nMost Similar Historical Days Analysis:")
        for i, day in enumerate(similar_days[:5]):
            print(f"\nPattern {i+1}:")
            print(f"Date: {day['date']}")
            print(f"Overall Similarity: {day['similarity']:.4f}")
            print(f"Weather Similarity: {day['weather_similarity']:.4f}")
            print(f"Clear Sky Similarity: {day['clear_sky_similarity']:.4f}")
            print(f"Ramp-up Similarity: {day['ramp_similarity']:.4f}")
            print(f"Ramp Rate: {day['ramp_rate']:.2f} W/m²/hour")
            print(f"Actual Value: {day['value']:.2f} W/m²")
        
        return similar_days
        
    except Exception as e:
        print(f"Error in find_similar_historical_patterns: {str(e)}")
        traceback.print_exc()
        return None

class WeatherPatternDetector:
    def __init__(self):
        self.cloud_indicators = {
            'humidity_threshold': 75,
            'pressure_drop_threshold': -0.05,
            'uv_drop_threshold': 2
        }
        
    def detect_patterns(self, historical_data, lookback_hours=3):
        """Enhanced weather pattern detection"""
        recent_data = historical_data.tail(lookback_hours)
        
        patterns = {
            'cloud_formation_likely': False,
            'clearing_likely': False,
            'stability': 'unstable',
            'confidence': 'low'
        }
        
        # Analyze rapid changes
        if len(recent_data) >= 2:
            # Humidity analysis
            humidity_trend = recent_data['Average Humidity'].diff().mean()
            humidity_level = recent_data['Average Humidity'].iloc[-1]
            
            # Pressure analysis
            pressure_trend = recent_data['Average Barometer'].diff().mean()
            
            # UV and radiation correlation
            uv_trend = recent_data['UV Index'].diff().mean()
            radiation_trend = recent_data['Solar Rad - W/m^2'].diff().mean()
            
            # Cloud formation indicators
            cloud_indicators = [
                humidity_trend > 0.2 and humidity_level > self.cloud_indicators['humidity_threshold'],
                pressure_trend < self.cloud_indicators['pressure_drop_threshold'],
                uv_trend < -self.cloud_indicators['uv_drop_threshold']
            ]
            
            # Clearing indicators
            clearing_indicators = [
                humidity_trend < -0.2,
                pressure_trend > 0.05,
                uv_trend > 1 and radiation_trend > 50
            ]
            
            patterns['cloud_formation_likely'] = sum(cloud_indicators) >= 2
            patterns['clearing_likely'] = sum(clearing_indicators) >= 2
            
            # Determine stability and confidence
            patterns['stability'] = 'stable' if abs(radiation_trend) < 50 else 'unstable'
            patterns['confidence'] = 'high' if abs(radiation_trend) > 100 else 'medium'
        
        return patterns

def adjust_morning_prediction(prediction, hour, prev_value, weather_patterns=None):
    """Adjust morning predictions based on patterns"""
    try:
        # Base multipliers for early morning hours
        base_multipliers = {6: 0.15, 7: 0.3, 8: 0.5, 9: 0.7}
        multiplier = base_multipliers.get(hour, 1.0)
        
        # Adjust multiplier based on weather patterns if available
        if weather_patterns is not None:
            if weather_patterns.get('clearing_likely', False):
                multiplier *= 1.3
            elif weather_patterns.get('cloud_formation_likely', False):
                multiplier *= 0.7
        
        # Apply multiplier and ensure reasonable limits
        adjusted_prediction = prediction * multiplier
        
        # Ensure prediction doesn't exceed reasonable limits
        if hour == 6:
            adjusted_prediction = min(max(adjusted_prediction, 10), 30)
        elif hour == 7:
            adjusted_prediction = min(max(adjusted_prediction, 50), 150)
        elif hour == 8:
            adjusted_prediction = min(max(adjusted_prediction, 100), 300)
        elif hour == 9:
            adjusted_prediction = min(max(adjusted_prediction, 200), 500)
        
        return adjusted_prediction
        
    except Exception as e:
        print(f"Error in adjust_morning_prediction: {str(e)}")
        return prediction

def adjust_afternoon_prediction(prediction, hour, prev_value, weather_patterns=None):
    """Adjust afternoon predictions based on patterns"""
    try:
        # Base decline factors
        base_decline = 0.8  # Less steep decline initially
        hours_past_peak = hour - 14  # Past 2 PM
        
        # Adjust decline based on weather patterns if available
        if weather_patterns is not None:
            if weather_patterns.get('cloud_formation_likely', False):
                base_decline = 0.7  # Steeper decline for cloudy conditions
            elif weather_patterns.get('clearing_likely', False):
                base_decline = 0.85  # Gentler decline for clear conditions
        
        # Calculate decline factor
        decline_factor = base_decline ** hours_past_peak
        
        # Apply decline
        adjusted_prediction = prediction * decline_factor
        
        # Ensure prediction doesn't exceed previous hour
        if prev_value > 0:
            max_allowed = prev_value * 0.9  # Maximum 90% of previous hour
            adjusted_prediction = min(adjusted_prediction, max_allowed)
        
        # Ensure reasonable minimum values
        if hour >= 17:  # Late afternoon
            adjusted_prediction = min(adjusted_prediction, 100)
        
        return max(0, adjusted_prediction)  # Ensure non-negative
        
    except Exception as e:
        print(f"Error in adjust_afternoon_prediction: {str(e)}")
        return prediction

def adjust_peak_prediction(prediction, hour, patterns, prev_value):
    """Adjust peak hour predictions"""
    if patterns['stability'] == 'unstable':
        if patterns['cloud_formation_likely']:
            return min(prediction, prev_value * 0.8)
        elif patterns['clearing_likely']:
            return max(prediction, prev_value * 1.2)
    return prediction

def adjust_afternoon_prediction(prediction, hour, patterns, prev_value):
    """Adjust afternoon predictions"""
    # Steeper decline in cloudy conditions
    if patterns['cloud_formation_likely']:
        decline_factor = 0.5
    else:
        decline_factor = 0.7
        
    hours_past_peak = hour - 14  # Past 2 PM
    prediction *= (decline_factor ** hours_past_peak)
    
    return min(prediction, prev_value * 0.9)  # Ensure declining trend

def analyze_early_warning_signs(data, last_timestamp):
    """Analyze early warning signs with more aggressive risk assessment"""
    try:
        # Get the last hour's data and previous data point
        last_hour_data = data[data['timestamp'].dt.hour == last_timestamp.hour].iloc[-1]
        prev_data = data.iloc[-2]
        
        risk_factors = {
            'high_risk': False,
            'risk_score': 0,
            'warning_signs': [],
            'critical_combinations': False
        }
        
        # 1. Enhanced Pressure Analysis
        pressure_trend = last_hour_data['Average Barometer'] - prev_data['Average Barometer']
        pressure_acceleration = pressure_trend - (prev_data['Average Barometer'] - data.iloc[-3]['Average Barometer'])
        
        if pressure_trend < -0.2:
            risk_factors['risk_score'] += 35
            risk_factors['warning_signs'].append(f"Significant pressure drop: {pressure_trend:.3f}")
        elif pressure_trend < -0.1:
            risk_factors['risk_score'] += 20
            risk_factors['warning_signs'].append(f"Moderate pressure drop: {pressure_trend:.3f}")
        
        if pressure_acceleration < -0.05:
            risk_factors['risk_score'] += 15
            risk_factors['warning_signs'].append(f"Accelerating pressure drop: {pressure_acceleration:.3f}")
            
        # 2. Enhanced Humidity Analysis
        current_humidity = last_hour_data['Average Humidity']
        humidity_trend = current_humidity - prev_data['Average Humidity']
        
        if current_humidity > 65:
            risk_score = min(int((current_humidity - 65) * 2), 30)
            risk_factors['risk_score'] += risk_score
            risk_factors['warning_signs'].append(f"Elevated humidity: {current_humidity:.1f}%")
        
        if humidity_trend > 0:
            risk_factors['risk_score'] += int(humidity_trend * 8)
            risk_factors['warning_signs'].append(f"Rising humidity: +{humidity_trend:.1f}%")
            
        # 3. Enhanced Temperature-Dew Point Analysis
        temp_dewpoint_spread = last_hour_data['Average Temperature'] - last_hour_data['Average Dew Point']
        if temp_dewpoint_spread < 5:
            risk_score = min(int((5 - temp_dewpoint_spread) * 15), 35)
            risk_factors['risk_score'] += risk_score
            risk_factors['warning_signs'].append(f"Small temp-dewpoint spread: {temp_dewpoint_spread:.1f}°C")
            
        # 4. Enhanced Wind Analysis
        wind_speed = last_hour_data['Avg Wind Speed - km/h']
        wind_change = wind_speed - prev_data['Avg Wind Speed - km/h']
        
        if wind_change > 2:
            risk_factors['risk_score'] += int(wind_change * 5)
            risk_factors['warning_signs'].append(f"Increasing wind: +{wind_change:.1f} km/h")
            
        # 5. Enhanced UV Index Analysis
        uv_index = last_hour_data['UV Index']
        expected_uv = get_expected_uv_for_hour(last_timestamp.hour)
        uv_ratio = uv_index / expected_uv if expected_uv > 0 else 1
        
        if uv_ratio < 0.9:
            risk_factors['risk_score'] += int((1 - uv_ratio) * 40)
            risk_factors['warning_signs'].append(f"Lower than expected UV: {uv_index} vs {expected_uv:.1f}")
            
        # 6. Critical Combinations Check
        critical_combinations = [
            (pressure_trend < -0.1 and humidity_trend > 0),
            (temp_dewpoint_spread < 4 and humidity_trend > 0),
            (pressure_acceleration < -0.05 and wind_change > 2),
            (uv_ratio < 0.9 and humidity_trend > 0),
            (pressure_trend < -0.15 and temp_dewpoint_spread < 5)
        ]
        
        if any(critical_combinations):
            risk_factors['critical_combinations'] = True
            risk_factors['risk_score'] = max(risk_factors['risk_score'], 75)
            risk_factors['warning_signs'].append("CRITICAL: Multiple high-risk indicators detected")
            
        # Set high risk flag if score is above threshold
        risk_factors['high_risk'] = risk_factors['risk_score'] >= 50
        
        print("\nEnhanced Early Warning Analysis:")
        print(f"Risk Score: {risk_factors['risk_score']}/100")
        print(f"Critical Combinations: {risk_factors['critical_combinations']}")
        print("Warning Signs Detected:")
        for warning in risk_factors['warning_signs']:
            print(f"- {warning}")
            
        return risk_factors
        
    except Exception as e:
        print(f"Error in analyze_early_warning_signs: {str(e)}")
        traceback.print_exc()
        return None

def get_expected_uv_for_hour(hour):
    """Get expected UV index for a given hour based on historical patterns"""
    # Simplified UV expectations for Davao (can be enhanced with historical data)
    uv_expectations = {
        6: 1, 7: 2, 8: 4, 9: 6, 10: 8, 11: 9, 12: 10, 
        13: 9, 14: 8, 15: 6, 16: 4, 17: 2, 18: 1
    }
    return uv_expectations.get(hour, 0)

def adjust_prediction_with_risk(pred, risk_factors, hour):
    """More aggressive prediction adjustment based on risk factors"""
    if not risk_factors or hour < 6 or hour > 18:
        return pred
        
    adjusted_pred = pred
    risk_score = risk_factors['risk_score']
    
    # Critical combinations trigger severe reductions
    if risk_factors['critical_combinations']:
        adjusted_pred *= 0.25  # 75% reduction for critical combinations
        print("CRITICAL combination of risk factors - applying 75% reduction")
    # Otherwise use graduated scale with more aggressive reductions
    elif risk_score >= 80:
        adjusted_pred *= 0.2  # 80% reduction
        print("Very high risk - applying 80% reduction")
    elif risk_score >= 60:
        adjusted_pred *= 0.3  # 70% reduction
        print("High risk - applying 70% reduction")
    elif risk_score >= 40:
        adjusted_pred *= 0.5  # 50% reduction
        print("Moderate risk - applying 50% reduction")
    elif risk_score >= 20:
        adjusted_pred *= 0.7  # 30% reduction
        print("Low risk - applying 30% reduction")
        
    return adjusted_pred

def analyze_minute_patterns(minute_data, last_timestamp, lookback_minutes=60):
    """Analyze high-frequency patterns in 5-minute data"""
    try:
        # Get recent data with longer lookback
        start_time = last_timestamp - pd.Timedelta(minutes=lookback_minutes)
        recent_data = minute_data[
            (minute_data['timestamp'] >= start_time) & 
            (minute_data['timestamp'] <= last_timestamp)            
        ]
        
        if recent_data.empty:
            return None
            
        # Sort by timestamp
        recent_data = recent_data.sort_values('timestamp')
        
        # Calculate high-frequency metrics
        patterns = {
            'extreme_risk': False,
            'warning_signs': [],
            'risk_score': 0
        }
        
        # 1. Analyze radiation stability
        radiation_changes = recent_data['Solar Rad - W/m^2'].diff()
        max_drop = radiation_changes.min()
        max_increase = radiation_changes.max()
        
        # Check for any significant drops in last hour
        if max_drop < -100:
            patterns['risk_score'] += 40
            patterns['warning_signs'].append(f"Recent significant drop: {max_drop:.1f} W/m²")
            
        # 2. UV Index variations
        uv_changes = recent_data['UV Index'].diff()
        if abs(uv_changes).max() > 1:
            patterns['risk_score'] += 30
            patterns['warning_signs'].append(f"UV Index unstable: {abs(uv_changes).max():.1f} change")
            
        # 3. Short-term trends
        last_15min = recent_data.tail(3)  # Last 15 minutes (3 x 5-minute intervals)
        if len(last_15min) >= 3:
            short_trend = last_15min['Solar Rad - W/m^2'].diff().mean()
            if short_trend < -20:
                patterns['risk_score'] += 35
                patterns['warning_signs'].append(f"Downward trend in last 15min: {short_trend:.1f} W/m²/5min")
        
        # 4. Analyze oscillations
        radiation_std = recent_data['Solar Rad - W/m^2'].std()
        if radiation_std > 50:
            patterns['risk_score'] += 25
            patterns['warning_signs'].append(f"High radiation variability: {radiation_std:.1f} W/m²")
            
        # 5. Check for critical combinations
        critical_conditions = [
            max_drop < -50 and recent_data['Average Humidity'].mean() > 65,
            abs(uv_changes).max() > 1 and max_drop < -30,
            radiation_std > 40 and recent_data['Average Humidity'].diff().mean() > 0
        ]
        
        if any(critical_conditions):
            patterns['extreme_risk'] = True
            patterns['risk_score'] = max(patterns['risk_score'], 90)
            patterns['warning_signs'].append("CRITICAL: Multiple destabilizing factors detected")
            
        print("\nHigh-frequency Pattern Analysis:")
        print(f"Maximum 5-min drop: {max_drop:.1f} W/m²")
        print(f"Maximum 5-min increase: {max_increase:.1f} W/m²")
        print(f"UV stability: {abs(uv_changes).max():.2f}")
        print(f"15-min trend: {short_trend if 'short_trend' in locals() else 'N/A'}")
        print(f"Radiation std: {radiation_std:.1f} W/m²")
        print(f"Risk Score: {patterns['risk_score']}/100")
        print("Warning Signs:")
        for warning in patterns['warning_signs']:
            print(f"- {warning}")
            
        return patterns
        
    except Exception as e:
        print(f"Error in analyze_minute_patterns: {str(e)}")
        return None

def adjust_prediction_with_minute_patterns(pred, minute_patterns):
    """Adjust prediction based on high-frequency patterns"""
    if not minute_patterns:
        return pred
        
    adjusted_pred = pred
    risk_score = minute_patterns['risk_score']
    
    # Extreme risk triggers severe reduction
    if minute_patterns['extreme_risk']:
        adjusted_pred *= 0.2  # 80% reduction
        print("EXTREME RISK - applying 80% reduction based on 5-minute patterns")
    # Otherwise use graduated scale
    elif risk_score >= 75:
        adjusted_pred *= 0.25  # 75% reduction
        print("Very high risk from 5-minute patterns - applying 75% reduction")
    elif risk_score >= 50:
        adjusted_pred *= 0.4  # 60% reduction
        print("High risk from 5-minute patterns - applying 60% reduction")
    elif risk_score >= 30:
        adjusted_pred *= 0.6  # 40% reduction
        print("Moderate risk from 5-minute patterns - applying 40% reduction")
        
    return adjusted_pred

def analyze_rapid_changes(minute_data, last_timestamp, lookback_minutes=15):
    """Analyze very recent data with enhanced sensitivity"""
    try:
        # Get recent data
        start_time = last_timestamp - pd.Timedelta(minutes=lookback_minutes)
        recent_data = minute_data[  
            (minute_data['timestamp'] >= start_time) & 
            (minute_data['timestamp'] <= last_timestamp)            
        ].copy()
        
        if recent_data.empty:
            return None
            
        recent_data = recent_data.sort_values('timestamp')
        
        # Initialize patterns
        patterns = {
            'severe_risk': False,
            'warning_signs': [],
            'reduction_factor': 1.0,
            'risk_level': 'low'
        }
        
        # 1. Pressure Analysis
        pressure_trend = recent_data['Average Barometer'].diff().mean()
        if pressure_trend < -0.1:  # More sensitive threshold
            patterns['warning_signs'].append(f"Rapid pressure drop: {pressure_trend:.3f}")
            patterns['reduction_factor'] *= 0.6
            
        # 2. Humidity Analysis
        current_humidity = recent_data['Average Humidity'].iloc[-1]
        if current_humidity > 65:  # Lower threshold
            humidity_factor = min((current_humidity - 65) * 0.02, 0.5)
            patterns['reduction_factor'] *= (1 - humidity_factor)
            patterns['warning_signs'].append(f"Elevated humidity: {current_humidity:.1f}%")
            
        # 3. UV Index Analysis
        uv_current = recent_data['UV Index'].iloc[-1]
        expected_uv = get_expected_uv_for_hour(last_timestamp.hour)
        uv_ratio = uv_current / expected_uv if expected_uv > 0 else 1
        
        if uv_ratio < 0.95:  # More sensitive UV threshold
            patterns['warning_signs'].append(f"UV below expected: {uv_current} vs {expected_uv}")
            patterns['reduction_factor'] *= 0.5
            
        # 4. Critical Combinations
        if pressure_trend < -0.1 and current_humidity > 65:
            patterns['severe_risk'] = True
            patterns['reduction_factor'] *= 0.3  # 70% reduction
            patterns['warning_signs'].append("CRITICAL: Pressure drop with high humidity")
            
        if uv_ratio < 0.95 and current_humidity > 65:
            patterns['severe_risk'] = True
            patterns['reduction_factor'] *= 0.25  # 75% reduction
            patterns['warning_signs'].append("CRITICAL: Low UV with high humidity")
            
        # Set risk level
        if patterns['severe_risk']:
            patterns['risk_level'] = 'severe'
        elif patterns['reduction_factor'] < 0.6:
            patterns['risk_level'] = 'high'
        elif patterns['reduction_factor'] < 0.8:
            patterns['risk_level'] = 'moderate'
            
        # Final reduction factor should never be above 0.8 if any warnings exist
        if patterns['warning_signs']:
            patterns['reduction_factor'] = min(patterns['reduction_factor'], 0.8)
            
        # Print analysis
        print("\nEnhanced Risk Analysis:")
        print(f"Risk Level: {patterns['risk_level']}")
        print(f"Reduction Factor: {patterns['reduction_factor']:.2f}")
        print("Warning Signs:")
        for warning in patterns['warning_signs']:
            print(f"- {warning}")
            
        return patterns
        
    except Exception as e:
        print(f"Error in analyze_rapid_changes: {str(e)}")
        return None

def analyze_recovery_patterns(data, last_timestamp):
    """Analyze potential recovery patterns with more conservative recovery factors"""
    try:
        # Get recent data - look at last 3 hours
        recent_data = data[data['timestamp'] <= last_timestamp].tail(3).copy()
        recent_data = recent_data.set_index('timestamp')
        
        recovery_patterns = {
            'likely_recovery': False,
            'recovery_factor': 1.0,
            'recovery_strength': 'none',
            'warning_signs': []
        }
        
        if len(recent_data) >= 2:
            # Calculate recent changes
            last_drop = recent_data['Solar Rad - W/m^2'].diff().iloc[-1]
            
            # Check if we had a sharp drop in the last hour
            if last_drop < -200:  # Significant drop
                # Get the magnitude of the drop
                drop_magnitude = abs(last_drop)
                
                # More conservative recovery based on drop magnitude
                if drop_magnitude > 500:  # Very large drop
                    max_recovery_factor = 1.5  # Limit recovery to 50% increase
                elif drop_magnitude > 300:
                    max_recovery_factor = 1.8  # Limit recovery to 80% increase
                else:
                    max_recovery_factor = 2.0  # Limit recovery to double
                
                # Check weather conditions for recovery
                current_humidity = recent_data['Average Humidity'].iloc[-1]
                humidity_trend = recent_data['Average Humidity'].diff().iloc[-1]
                pressure_trend = recent_data['Average Barometer'].diff().iloc[-1]
                uv_trend = recent_data['UV Index'].diff().iloc[-1]
                
                # Enhanced recovery conditions
                recovery_conditions = [
                    humidity_trend < 0,  # Humidity decreasing
                    pressure_trend > 0,  # Pressure increasing
                    current_humidity < 75,  # Not too humid
                    uv_trend >= 0  # UV stable or increasing
                ]
                
                recovery_score = sum(recovery_conditions)
                
                # More conservative recovery factors
                if recovery_score >= 3:
                    recovery_patterns['likely_recovery'] = True
                    recovery_patterns['recovery_factor'] = min(2.0, max_recovery_factor)
                    recovery_patterns['recovery_strength'] = 'moderate'
                    recovery_patterns['warning_signs'].append(
                        f"Moderate recovery likely - limited to {max_recovery_factor}x due to drop magnitude"
                    )
                elif recovery_score >= 2:
                    recovery_patterns['likely_recovery'] = True
                    recovery_patterns['recovery_factor'] = min(1.5, max_recovery_factor)
                    recovery_patterns['recovery_strength'] = 'mild'
                    recovery_patterns['warning_signs'].append("Mild recovery possible")
                else:
                    recovery_patterns['recovery_factor'] = min(1.3, max_recovery_factor)
                    recovery_patterns['recovery_strength'] = 'weak'
                    recovery_patterns['warning_signs'].append("Weak recovery expected")
                
                # Add drop magnitude to warning signs
                recovery_patterns['warning_signs'].append(
                    f"Recent drop magnitude: {drop_magnitude:.1f} W/m²"
                )
        
        return recovery_patterns
        
    except Exception as e:
        print(f"Error in analyze_recovery_patterns: {str(e)}")
        return None

def analyze_trend_sequence(data, last_timestamp):
    """Analyze sequence of changes to detect patterns"""
    try:
        # Get last 3 hours of data
        recent_data = data[data['timestamp'] <= last_timestamp].tail(3).copy()
        
        trend_analysis = {
            'pattern': 'unknown',
            'strength': 'moderate',
            'adjustment_factor': 1.0,
            'warnings': []
        }
        
        if len(recent_data) >= 2:
            # Calculate changes
            changes = recent_data['Solar Rad - W/m^2'].diff().values
            
            # Detect recovery after drop
            if len(changes) >= 2:
                last_change = changes[-1]
                prev_change = changes[-2]
                
                if prev_change < -200 and last_change > 100:  # Drop followed by recovery
                    trend_analysis['pattern'] = 'recovery_after_drop'
                    trend_analysis['warnings'].append("Recovery pattern after significant drop")
                    
                    # Calculate recovery ratio
                    recovery_ratio = abs(last_change / prev_change)
                    if recovery_ratio > 0.5:  # Strong recovery
                        trend_analysis['strength'] = 'strong'
                        trend_analysis['adjustment_factor'] = 0.7  # Expect 30% reduction
                    else:  # Moderate recovery
                        trend_analysis['strength'] = 'moderate'
                        trend_analysis['adjustment_factor'] = 0.6  # Expect 40% reduction
                        
                elif last_change < -200:  # Recent sharp drop
                    trend_analysis['pattern'] = 'sharp_drop'
                    trend_analysis['warnings'].append("Recent sharp drop")
                    trend_analysis['adjustment_factor'] = 0.4  # Expect 60% reduction
                    
                elif last_change < -100:  # Moderate drop
                    trend_analysis['pattern'] = 'moderate_drop'
                    trend_analysis['warnings'].append("Recent moderate drop")
                    trend_analysis['adjustment_factor'] = 0.5  # Expect 50% reduction
        
        print("\nTrend Sequence Analysis:")
        print(f"Pattern: {trend_analysis['pattern']}")
        print(f"Strength: {trend_analysis['strength']}")
        print(f"Suggested adjustment: {trend_analysis['adjustment_factor']:.2f}")
        print("Warnings:", ", ".join(trend_analysis['warnings']))
        
        return trend_analysis
        
    except Exception as e:
        print(f"Error in analyze_trend_sequence: {str(e)}")
        return None

class CloudFeatures:
    """Class to handle cloud-related feature calculations"""
    def __init__(self):
        self.cloud_threshold = 0.7
        self.radiation_drop_threshold = 200  # W/m²
        self.sudden_change_window = 3  # hours

    def calculate_cloud_features(self, data):
        """Calculate cloud-related features from weather data"""
        features = {
            'sudden_drop': False,
            'sudden_increase': False,
            'cloud_cover': False
        }
        
        if isinstance(data, torch.Tensor):
            data = data.numpy()
            
        # Calculate radiation changes
        if len(data.shape) > 1 and data.shape[0] > 1:
            radiation_changes = np.diff(data[:, -1])  # Assuming last column is radiation
            features['sudden_drop'] = np.any(radiation_changes < -self.radiation_drop_threshold)
            features['sudden_increase'] = np.any(radiation_changes > self.radiation_drop_threshold)
            
            # Detect cloud cover from radiation pattern
            if len(radiation_changes) >= self.sudden_change_window:
                std_radiation = np.std(data[:, -1])
                mean_radiation = np.mean(data[:, -1])
                if mean_radiation > 0:
                    variation_coeff = std_radiation / mean_radiation
                    features['cloud_cover'] = variation_coeff > self.cloud_threshold
        
        return features

def calculate_clear_sky_radiation(hour, latitude, longitude, date):
    """Calculate theoretical clear sky radiation"""
    # Convert to radians
    lat_rad = np.radians(latitude)
    
    # Day of year
    day_of_year = date.timetuple().tm_yday
    
    # Solar declination
    declination = 23.45 * np.sin(np.radians(360/365 * (day_of_year - 81)))
    declination_rad = np.radians(declination)
    
    # Hour angle
    hour_angle = 15 * (hour - 12)  # 15 degrees per hour
    hour_angle_rad = np.radians(hour_angle)
    
    # Solar altitude
    sin_altitude = (np.sin(lat_rad) * np.sin(declination_rad) + 
                   np.cos(lat_rad) * np.cos(declination_rad) * 
                   np.cos(hour_angle_rad))
    
    # Solar constant
    solar_constant = 1361  # W/m² 
    # Atmospheric transmission
    transmission = 0.7  # Typical clear sky value
    
    # Calculate clear sky radiation
    clear_sky = solar_constant * sin_altitude * transmission
    
    return max(0, clear_sky)  # Ensure non-negative values

def predict_with_history(model, data, minute_data, scaler_X, scaler_y, base_features, feature_averages):
    try:
        # Get current timestamp and hour
        last_timestamp = data['timestamp'].max()
        current_hour = last_timestamp.hour
        current_date = last_timestamp.date()

        # Create prediction dataframe
        prediction_data = pd.DataFrame({
            'hour': range(24),
            'predictions': np.nan
        })

        # Make predictions for each hour
        for hour in range(current_hour + 1):
            if hour < 6 or hour > 18:  # Skip night hours
                prediction_data.loc[prediction_data['hour'] == hour, 'predictions'] = 0
                continue
                
            # Get historical data
            historical_data = data[
                (data['timestamp'].dt.date == current_date) & 
                (data['hour'] < hour)
            ]
            
            if not historical_data.empty:
                # Create feature vector
                last_data = historical_data.iloc[-1].copy()
                feature_vector = []
                for feature in base_features:
                    feature_vector.append(float(last_data[feature]))
                
                # Scale features
                X = np.array([feature_vector])
                X_scaled = scaler_X.transform(X)
                
                # Calculate time features
                hour_sin = np.sin(2 * np.pi * hour/24)
                hour_cos = np.cos(2 * np.pi * hour/24)
                time_features = np.array([[hour_sin, hour_cos]])
                
                # Make prediction
                model.eval()
                with torch.no_grad():
                    prediction = model(
                        torch.FloatTensor(X_scaled),
                        torch.FloatTensor(time_features)    
                    )
                    prediction = scaler_y.inverse_transform(prediction.numpy())[0][0]
                
                # Apply afternoon corrections
                if hour >= 14:
                    # Get peak value for the day
                    peak_value = historical_data['Solar Rad - W/m^2'].max()
                    hours_after_peak = hour - 12  # Assuming peak at noon
                    
                    # Calculate decline factor
                    base_decline = 0.65  # Steeper decline
                    decline_factor = base_decline ** hours_after_peak
                    
                    # Apply decline
                    prediction *= decline_factor
                    
                    # Additional limit based on previous hour
                    if hour > 14:
                        prev_value = prediction_data.loc[prediction_data['hour'] == hour - 1, 'predictions'].iloc[0]
                        max_allowed = prev_value * 0.85  # Maximum 85% of previous hour
                        prediction = min(prediction, max_allowed)
                
                # Store prediction
                prediction_data.loc[prediction_data['hour'] == hour, 'predictions'] = prediction

        return prediction_data
        
    except Exception as e:
        print(f"Error in predict_with_history: {str(e)}")
        traceback.print_exc()
        return None

def analyze_daily_patterns(data, hour):
    """Analyze typical daily patterns and identify key transition periods"""
    
    daily_patterns = {
        # Morning ramp-up (6:00-10:00)
        'morning_ramp': {
            'hours': range(6, 11),
            'expected_trend': 'increasing',
            'typical_increase': 150,  # W/m² per hour
            'vulnerability': 'medium'  # Moderate chance of cloud interference
        },
        
        # Peak hours (10:00-14:00)
        'peak_hours': {
            'hours': range(10, 15),
            'expected_trend': 'stable_high',
            'typical_range': (600, 900),  # W/m²
            'vulnerability': 'high'  # High chance of sudden drops
        },
        
        # Afternoon decline (14:00-18:00)
        'afternoon_decline': {
            'hours': range(14, 19),
            'expected_trend': 'decreasing',
            'typical_decrease': 100,  # W/m² per hour
            'vulnerability': 'medium'
        }
    }
    
    # Identify current period
    current_period = None
    for period, info in daily_patterns.items():
        if hour in info['hours']:
            current_period = period
            break
    
    return current_period, daily_patterns

def predict_transitions(data, last_timestamp, current_patterns):
    """Predict potential transitions in solar radiation"""
    
    hour = last_timestamp.hour
    current_period, daily_patterns = analyze_daily_patterns(data, hour)
    
    # Get recent values
    recent_data = data[data['timestamp'] <= last_timestamp].tail(3)
    recent_values = recent_data['Solar Rad - W/m^2'].values
    
    transitions = {
        'likely_change': None,
        'change_magnitude': 'none',
        'confidence': 'low',
        'warning_signs': []
    }
    
    if current_period == 'peak_hours':
        # Check for conditions that might lead to drops
        if current_patterns['cloud_cover']:
            transitions['likely_change'] = 'decrease'
            transitions['change_magnitude'] = 'significant'
            transitions['confidence'] = 'high'
            transitions['warning_signs'].append("Cloud cover during peak hours")
            
        # Check for recovery conditions
        elif len(recent_values) >= 2 and recent_values[-2] < recent_values[-1]:
            transitions['likely_change'] = 'increase'
            transitions['change_magnitude'] = 'moderate'
            transitions['confidence'] = 'medium'
            transitions['warning_signs'].append("Recovery pattern after drop")
    
    elif current_period == 'morning_ramp':
        # Check if the ramp-up is being interrupted
        expected_increase = daily_patterns['morning_ramp']['typical_increase']
        actual_increase = recent_values[-1] - recent_values[-2] if len(recent_values) >= 2 else 0
        
        if actual_increase < expected_increase * 0.5:
            transitions['likely_change'] = 'decrease'
            transitions['change_magnitude'] = 'moderate'
            transitions['confidence'] = 'medium'
            transitions['warning_signs'].append("Morning ramp-up interrupted")
    
    print("\nTransition Analysis:")
    print(f"Current period: {current_period}")
    print(f"Likely change: {transitions['likely_change']}")
    print(f"Change magnitude: {transitions['change_magnitude']}")
    print(f"Confidence: {transitions['confidence']}")
    print("Warning signs:", ", ".join(transitions['warning_signs']))
    
    return transitions

def generate_simulated_solcast_data(ground_data):
    """Load existing Solcast comparison data"""
    try:
        print("\nLoading Solcast comparison data...")
        
        # Load the existing comparison data
        solcast_df = pd.read_csv('solcast_ground_comparison.csv')
        solcast_df['timestamp'] = pd.to_datetime(solcast_df['timestamp'])
        
        print(f"Loaded {len(solcast_df)} records of Solcast data")
        return solcast_df
        
    except Exception as e:
        print(f"Error loading Solcast data: {str(e)}")
        traceback.print_exc()
        return None

def merge_and_validate_data(ground_data, solcast_data):
    """Merge and validate ground-based and Solcast data"""
    try:
        print("\nMerging ground-based and Solcast data...")
        
        # Ensure timestamps are in the same format
        ground_data['timestamp'] = pd.to_datetime(ground_data['Date & Time'])
        
        # Keep the original 'Solar Rad - W/m^2' from ground data and merge Solcast features
        merged_data = pd.merge(
            ground_data[['timestamp', 'Date & Time', 'Solar Rad - W/m^2', 'Average Temperature', 
                        'Average Humidity', 'UV Index', 'Average Barometer', 'Average Dew Point',
                        'Average Wet Bulb', 'Avg Wind Speed - km/h', 'Average Wind Chill',
                        'Heat Index', 'Average THW Index', 'Average THSW Index']],
            solcast_data[['timestamp', 'clear_sky_radiation', 'cloud_cover', 
                         'clearness_index', 'cloud_impact', 'data_quality']],
            on='timestamp',
            how='left'
        )
        
        # Print validation summary
        print("\nData Validation Summary:")
        print(f"Total records: {len(merged_data)}")
        print(f"Records with good quality (>0.8): {(merged_data['data_quality'] > 0.8).sum()}")
        print(f"Average clearness index: {merged_data['clearness_index'].mean():.3f}")
        print(f"Average cloud cover: {merged_data['cloud_cover'].mean():.1f}%")
        
        # Print column names for debugging
        print("\nAvailable columns after merge:")
        print(merged_data.columns.tolist())
        
        return merged_data
        
    except Exception as e:
        print(f"Error in merge_and_validate_data: {str(e)}")
        traceback.print_exc()
        return None

def predict_historical_hours(model, data, target_date):
    try:
        # Get data for target date
        day_data = data[data['timestamp'].dt.date == target_date].copy()
        if day_data.empty:
            return None
            
        current_hour = day_data['hour'].max()
        predictions = []
        
        print("\nHistorical Predictions Analysis:")
        print("================================")
        
        # Predict each hour from 0 to current_hour-1
        for hour in range(current_hour):
            try:
                # Get actual value for this hour directly from dataset
                hour_data = day_data[day_data['hour'] == hour]
                if hour_data.empty:
                    continue
                    
                actual_value = hour_data['Solar Rad - W/m^2'].iloc[0]
                
                # Get previous hour value
                prev_hour_data = day_data[day_data['hour'] == hour - 1]
                prev_value = prev_hour_data['Solar Rad - W/m^2'].iloc[0] if not prev_hour_data.empty else 0
                
                # Prepare base features first
                feature_vector = []
                for feature in base_features:
                    if feature in hour_data.columns:
                        feature_vector.append(float(hour_data[feature].iloc[0]))
                    else:
                        print(f"Warning: Missing feature {feature}")
                        feature_vector.append(0.0)
                
                # Add time features
                hour_sin = np.sin(2 * np.pi * hour / 24)
                hour_cos = np.cos(2 * np.pi * hour / 24)
                
                # Combine all features
                X = np.array([feature_vector + [hour_sin, hour_cos]])
                
                # Scale features
                X_scaled = scaler_X.transform(X)
                
                # Make prediction
                prediction = predict_next_hour(model, X_scaled, hour, prev_value, data, target_date)
                
                # Store prediction if valid
                if prediction is not None:
                    predictions.append({
                        'hour': hour,
                        'timestamp': f"{target_date} {hour:02d}:00",
                        'actual': actual_value,
                        'predicted': prediction,
                        'absolute_error': abs(prediction - actual_value),
                        'relative_error': (abs(prediction - actual_value) / actual_value * 100) if actual_value != 0 else 0
                    })
                    
                    print(f"\nHour {hour:02d}:00")
                    print(f"Actual: {actual_value:.2f} W/m²")
                    print(f"Predicted: {prediction:.2f} W/m²")
                    print(f"Absolute Error: {abs(prediction - actual_value):.2f} W/m²")
                    print(f"Relative Error: {(abs(prediction - actual_value) / actual_value * 100) if actual_value != 0 else 0:.1f}%")
                
            except Exception as hour_error:
                print(f"Error processing hour {hour}: {str(hour_error)}")
                continue
        
        if not predictions:
            print("No valid predictions generated")
            return None
            
        # Create DataFrame with results
        results_df = pd.DataFrame(predictions)
        
        # Calculate summary statistics
        mean_abs_error = results_df['absolute_error'].mean()
        mean_rel_error = results_df['relative_error'].mean()
        
        print("\nSummary Statistics:")
        print(f"Mean Absolute Error: {mean_abs_error:.2f} W/m²")
        print(f"Mean Relative Error: {mean_rel_error:.1f}%")
        
        return results_df
        
    except Exception as e:
        print(f"Error in predict_historical_hours: {str(e)}")
        traceback.print_exc()
        return None

def plot_predictions(results_df, save_path):
    """Create visualization of actual vs predicted values"""
    plt.figure(figsize=(12, 6))
    
    # Get valid data points (where actual values exist)
    valid_data = results_df.dropna(subset=['actual'])
    
    # Plot actual values
    plt.plot(valid_data['hour'], valid_data['actual'], 
             label='Actual', marker='o', color='blue')
    
    # Plot all predictions
    plt.plot(results_df['hour'], results_df['predicted'], 
             label='Predicted', marker='x', color='red', linestyle='--')
    
    plt.title('Solar Radiation: Actual vs Predicted Values')
    plt.xlabel('Hour')
    plt.ylabel('Solar Radiation (W/m²)')
    plt.legend()
    plt.grid(True)
    
    # Add value labels for all points, including next hour prediction
    for idx, row in results_df.iterrows():
        # Add actual value label if it exists
        if pd.notna(row['actual']):
            plt.annotate(f'{row["actual"]:.0f}', 
                        (row['hour'], row['actual']),
                        textcoords="offset points",
                        xytext=(0,10),
                        ha='center')
        
        # Add predicted value label for all points, including next hour
        plt.annotate(f'{row["predicted"]:.0f}',
                    (row['hour'], row['predicted']),
                    textcoords="offset points",
                    xytext=(0,-15),
                    ha='center')
    
    # Save plot
    plt.savefig(save_path, bbox_inches='tight', dpi=300)
    plt.close()

def calculate_multi_timeframe_trends(historical_data):
    """Calculate trends over different timeframes"""
    trends = {}
    
    # Last hour trend
    if len(historical_data) >= 2:
        trends['last_hour'] = historical_data['Solar Rad - W/m^2'].diff().iloc[-1]
    else:
        trends['last_hour'] = 0
        
    # 3-hour trend
    if len(historical_data) >= 6:
        trends['3_hour'] = (historical_data['Solar Rad - W/m^2'].iloc[-1] - 
                          historical_data['Solar Rad - W/m^2'].iloc[-6]) / 3
    else:
        trends['3_hour'] = 0
        
    # Morning trend (since 6 AM)
    morning_data = historical_data[historical_data['hour'] >= 6]
    if not morning_data.empty:
        trends['morning'] = morning_data['Solar Rad - W/m^2'].diff().mean()
    else:
        trends['morning'] = 0
        
    return trends

def analyze_weather_changes(historical_data):
    """Analyze changes in weather conditions"""
    changes = {}
    
    if len(historical_data) >= 2:
        # Cloud trend based on UV and radiation
        changes['cloud_trend'] = (
            historical_data['UV Index'].diff().mean() +
            historical_data['Solar Rad - W/m^2'].diff().mean() / 100
        )
        
        # Humidity trend
        changes['humidity_trend'] = historical_data['Average Humidity'].diff().mean()
        
        # Temperature trend
        changes['temp_trend'] = historical_data['Average Temperature'].diff().mean()
        
        # Pressure trend
        changes['pressure_trend'] = historical_data['Average Barometer'].diff().mean()
    else:
        changes = {
            'cloud_trend': 0,
            'humidity_trend': 0,
            'temp_trend': 0,
            'pressure_trend': 0
        }
    
    return changes

def predict_clear_sky_ratio(hour, conditions, trends, weather_changes):
    """Predict the ratio of actual to clear sky radiation"""
    base_ratio = 0.7  # Default ratio
    
    # Adjust for time of day
    if 10 <= hour <= 14:  # Peak hours
        base_ratio = 0.8
    elif hour < 8 or hour > 16:  # Early morning/late afternoon
        base_ratio = 0.6
        
    # Adjust for weather conditions
    if conditions['UV Index'] > 8:
        base_ratio += 0.1
    elif conditions['UV Index'] < 4:
        base_ratio -= 0.2
        
    # Adjust for trends
    if trends['last_hour'] > 0:
        base_ratio += 0.05
    elif trends['last_hour'] < 0:
        base_ratio -= 0.1
        
    # Adjust for weather changes
    if weather_changes['cloud_trend'] < 0:  # Clearing
        base_ratio += 0.1
    elif weather_changes['cloud_trend'] > 0:  # Clouding
        base_ratio -= 0.15
        
    # Ensure ratio is within reasonable bounds
    return max(0.1, min(0.95, base_ratio))

def augment_training_data(X, y):
    """Augment training data with synthetic transitions"""
    aug_X = []
    aug_y = []
    
    for i in range(len(X)-1):
        if abs(y[i+1] - y[i]) > 100:  # Large transition
            # Create intermediate points
            alpha = np.linspace(0, 1, 5)[1:-1]
            for a in alpha:
                aug_X.append(X[i] * (1-a) + X[i+1] * a)
                aug_y.append(y[i] * (1-a) + y[i+1] * a)
    
    if aug_X:
        X = np.vstack([X, np.array(aug_X)])
        y = np.concatenate([y, np.array(aug_y)])
    
    return X, y

def predict_next_hour(model, X, hour, prev_value, data, target_date):
    try:
        # Verify input dimensions
        if X.shape[1] != 12:  # 10 weather features + 2 time features
            raise ValueError(f"Expected 12 features, got {X.shape[1]}")
        
        # Split features
        weather_features = torch.FloatTensor(X[:, :-2])  # First 10 features
        time_features = torch.FloatTensor(X[:, -2:])     # Last 2 features
        
        # Night hours (0-5, 18-23) should be zero
        if hour < 6 or hour >= 18:
            return 0.0
            
        # Make base prediction
        model.eval()
        with torch.no_grad():
            prediction = model(weather_features, time_features)
            if prediction is None:
                return None
                
            # Inverse transform the prediction to get actual scale
            prediction = scaler_y.inverse_transform(prediction.numpy())[0][0]
        
        # Get clear sky radiation for reference
        clear_sky = calculate_clear_sky_radiation(
            hour, 
            DAVAO_LATITUDE, 
            DAVAO_LONGITUDE, 
            target_date
        )
        
        # Early morning adjustments (6-7 AM)
        if hour == 6:
            # Use clear sky radiation as reference, expect 5-10% of clear sky
            min_value = max(10, clear_sky * 0.05)  # At least 10 W/m²
            max_value = min(30, clear_sky * 0.10)  # At most 30 W/m² or 10% of clear sky
            prediction = max(min_value, min(prediction, max_value))
            
        elif hour == 7:
            # Use clear sky radiation as reference, expect 15-25% of clear sky
            min_value = max(50, clear_sky * 0.15)  # At least 50 W/m²
            max_value = min(150, clear_sky * 0.25)  # At most 150 W/m² or 25% of clear sky
            prediction = max(min_value, min(prediction, max_value))
            
            # Consider previous hour trend
            if prev_value > 0:
                max_increase = 120  # Maximum 120 W/m² increase from previous hour
                prediction = min(prediction, prev_value + max_increase)
        
        elif hour >= 8 and hour <= 16:
            # Ensure prediction doesn't exceed clear sky radiation
            prediction = min(prediction, clear_sky * 1.1)  # Allow slight exceed for edge cases
            
            # Ensure reasonable minimum based on time of day
            if 10 <= hour <= 14:  # Peak hours
                prediction = max(prediction, clear_sky * 0.3)  # At least 30% of clear sky
            else:
                prediction = max(prediction, clear_sky * 0.1)  # At least 10% of clear sky
        
        return prediction
        
    except Exception as e:
        print(f"Error in predict_next_hour: {str(e)}")
        traceback.print_exc()
        return None

def main():
    try:
        print("Starting solar radiation prediction pipeline...")
        
        # Load and validate input data
        if not os.path.exists('dataset.csv'):
            raise FileNotFoundError("dataset.csv not found")
        
        print("\nLoading and preprocessing data...")
        data, last_date, feature_averages = preprocess_data('dataset.csv')
        if data is None:
            raise ValueError("Failed to preprocess data")
        
        print("\nLoading 5-minute interval data...")
        minute_data = extract_minute_data('dataset.csv')
        if minute_data is None:
            raise ValueError("Failed to load 5-minute interval data")
            
        # Prepare features
        print("\nPreparing features...")
        X, y, feature_names = prepare_features(data)
        if X is None or y is None:
            raise ValueError("Feature preparation failed")
            
        print(f"Feature dimensions: {X.shape}")
        
        # Scale features
        X_scaled = scaler_X.fit_transform(X)
        y_scaled = scaler_y.fit_transform(y.reshape(-1, 1))
        
        # Split data ensuring consistent feature dimensions
        train_size = int(0.8 * len(X_scaled))
        X_train = X_scaled[:train_size]
        X_test = X_scaled[train_size:]
        y_train = y_scaled[:train_size]
        y_test = y_scaled[train_size:]
        
        print(f"\nTraining data dimensions:")
        print(f"X_train: {X_train.shape}")
        print(f"X_test: {X_test.shape}")
        
        # In main(), before training:
        print("\nAugmenting training data...")
        X_train, y_train = augment_training_data(X_train, y_train)
        
        # Train model
        print("\nTraining model...")
        model, _, _, _, _ = train_model(X_train, y_train, X_test, y_test, scaler_y)
        
        if model is not None:
            # Get current timestamp and data
            last_timestamp = data['timestamp'].max()
            current_hour = last_timestamp.hour
            target_date = last_timestamp.date()
            
            # Make historical predictions first
            print("\nGenerating Historical Predictions...")
            historical_results = predict_historical_hours(model, data, target_date)
            
            # Get current hour data and make prediction for it
            current_data = data[data['timestamp'] == last_timestamp].iloc[0]
            current_actual = current_data['Solar Rad - W/m^2']
            
            # Prepare base features first (10 weather features)
            feature_vector = []
            for feature in base_features:
                if feature in current_data:
                    feature_vector.append(float(current_data[feature]))
                else:
                    print(f"Warning: Missing feature {feature}, using 0")
                    feature_vector.append(0.0)
            
            # Add time features (2 features)
            hour_sin = np.sin(2 * np.pi * current_hour / 24)
            hour_cos = np.cos(2 * np.pi * current_hour / 24)
            
            # Combine all features (10 + 2 = 12 features)
            X = np.array([feature_vector + [hour_sin, hour_cos]])
            
            print(f"Feature vector shape before scaling: {X.shape}")
            print("Features included:", base_features + ['hour_sin', 'hour_cos'])
            
            # Scale features
            X_scaled = scaler_X.transform(X)
            
            # Make prediction
            prediction = predict_next_hour(model, X_scaled, current_hour + 1, current_actual, data, target_date)
            
            # Add current hour to historical results with proper error handling
            if historical_results is not None and prediction is not None:
                current_hour_data = pd.DataFrame([{
                    'hour': current_hour + 1,
                    'timestamp': f"{target_date} {current_hour+1:02d}:00",
                    'actual': current_actual,
                    'predicted': prediction,
                    'absolute_error': abs(prediction - current_actual) if prediction is not None else None,
                    'relative_error': (abs(prediction - current_actual) / current_actual * 100) 
                                if prediction is not None and current_actual != 0 else None
                }])
                
                historical_results = pd.concat([historical_results, current_hour_data], ignore_index=True)
            
            # Then make next hour prediction using same feature preparation method
            feature_vector = []
            for feature in base_features:
                feature_vector.append(float(current_data[feature]))
            
            # Add time features (2 features)
            hour_sin = np.sin(2 * np.pi * (current_hour + 1) / 24)
            hour_cos = np.cos(2 * np.pi * (current_hour + 1) / 24)
            
            # Combine all features (10 + 2 = 12 features)
            X = np.array([feature_vector + [hour_sin, hour_cos]])
            
            print(f"Feature vector shape before scaling: {X.shape}")
            print("Features included:", base_features + ['hour_sin', 'hour_cos'])
            
            # Scale features
            X_scaled = scaler_X.transform(X)
            
            # Make prediction
            prediction = predict_next_hour(model, X_scaled, current_hour + 1, current_actual, data, target_date)
            
            # Create combined results DataFrame
            if historical_results is not None:
                next_hour_pred = pd.DataFrame([{
                    'hour': current_hour + 1,
                    'timestamp': f"{target_date} {current_hour+1:02d}:00",
                    'actual': None,  # Future value not known
                    'predicted': prediction,
                    'absolute_error': None,
                    'relative_error': None
                }])
                
                combined_results = pd.concat([historical_results, next_hour_pred], ignore_index=True)
                
                # Sort by hour to ensure correct order
                combined_results = combined_results.sort_values('hour').reset_index(drop=True)
                
                # Save combined results
                combined_results.to_csv('figures/hourly_predictions.csv', index=False)
                
                # Create visualization
                plot_predictions(combined_results, 'figures/hourly_predictions.png')
            
            # Print comprehensive prediction analysis
            print("\nComprehensive Prediction Analysis:")
            print(f"Current Hour: {current_hour}:00")
            print(f"Current Hour Actual: {current_actual:.2f} W/m²")
            print(f"Current Hour Predicted: {prediction:.2f} W/m²")
            print(f"Current Hour Error: {abs(prediction - current_actual):.2f} W/m²")
            print(f"\nPredicting for: {current_hour+1}:00")
            print(f"Previous Hour Value: {current_actual:.2f} W/m²")
            
            print("\nPrediction Details:")
            print(f"Final Predicted Value: {prediction:.2f} W/m²")
        
        else:
            print("\nError: Model training failed")
            
    except Exception as e:
        print(f"Error in main: {str(e)}")
        traceback.print_exc()
    finally:
        plt.close('all')  # Clean up any open plots

if __name__ == "__main__":
    main()