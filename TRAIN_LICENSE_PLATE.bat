@echo off
echo ========================================================
echo   Training YOLOv8 on License Plate Dataset
echo ========================================================
cd c:\Users\19923\Desktop\SECS\AR-PPE-Detection
python train.py --epochs 30 --batch 8 --name license_plate_model --base-model yolov8s.pt
echo.
echo ========================================================
echo   Training Finished!
echo   To use this model, please ensure config.yaml has:
echo   detector_backend: finetuned
echo ========================================================
pause
