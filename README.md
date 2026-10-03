# COMP2019 Group 2

## Project Information

**Module:** COMP2019 – Software Engineering Group Project  
**Academic Year:** 2026–2027  
**Group:** Group 002

### Project Title

**Intelligent Traffic Monitoring and Vehicle Plate Anomaly Detection System via Augmented Reality**

---

## Team Members

| Name | Student ID | University Email |
|---|---:|---|
| Yifei Xie | 20796507 | `hcyyx2@nottingham.edu.my` |
| Shiyu Cao | 20800646 | `hcysc4@nottingham.edu.my` |
| Hanwen Zhang | 20806628 | `hcyhz2@nottingham.edu.my` |
| Junhao Li | 20806655 | `hcyjl11@nottingham.edu.my` |
| Wasi Ur Rehman | 20810528 | `hcywr1@nottingham.edu.my` |
---

## Project Supervision

**Academic Supervisor:** Dr Doreen Ying Ying Sim  
**Client:** School of Civil Engineering  
**Industry Supervisor:** Dr Abdullahi Ali Mohamed

---

## Project Overview

The project aims to develop an intelligent software system for **vehicle licence plate anomaly detection and traffic monitoring**, with information potentially presented through **Augmented Reality (AR)**.

The proposed system is intended to assist **traffic police and security guards** in monitoring vehicle licence plates, traffic flow, and potentially abnormal or reckless driving behaviour.

The project combines several areas, including **Computer Vision, object tracking, Machine Learning and Deep Learning**, to process vehicle and traffic information over time.

---

## Main Project Areas

### 1. Vehicle Licence Plate Monitoring

The system is expected to support the detection and monitoring of vehicle licence plates, including cases such as:

- Licensed vehicle plates
- Unmatched plates
- Expired registrations
- Vehicle plate anomalies
- Illegal vehicle plate numbers
- Duplicate entries

### 2. Traffic Flow Monitoring

The system should support traffic monitoring functions such as:

- Real-time vehicle counting
- Traffic density measurement
- Tracking traffic movement across road lanes
- Monitoring vehicle flow over time
- Avoiding duplicate vehicle counts

### 3. Vehicle Tracking

Computer Vision and object-tracking techniques may be used to track vehicles over time.

The project brief specifically identifies technologies and algorithms such as:

- OpenCV
- ByteTrack
- DeepSORT

These may be investigated for tracking individual vehicles and preventing the same vehicle from being counted multiple times.

### 4. Traffic and Driving Behaviour Analysis

The proposed system may also support:

- Detection of illegal driving
- Detection of reckless driving behaviour
- Analysis of traffic flow
- Prediction of traffic congestion during peak hours

Machine Learning and Deep Learning techniques may be explored for these functions.

### 5. Augmented Reality

Augmented Reality (AR) is proposed as a way of presenting relevant vehicle, licence plate, anomaly, and traffic information to users.

The exact role and scope of the AR component will be refined as the project requirements are clarified.

### 6. Possible Extended Features

If feasible within the project scope, the project brief also suggests exploring:

- Generative AI
- Agentic AI

These are considered potential additional features for improving user experience rather than established core functionality at this stage.

---

## Intended Users

According to the initial project brief, the system is intended to support users such as:

- **Traffic police**
- **Security guards**

The system should help users keep track of registered vehicle plates, monitor traffic flow, and identify potentially unmatched, expired, illegal, or otherwise anomalous vehicle plates and reckless driving behaviour.

---

## High-Level System Concept

```text
Traffic Camera / Video
          |
          v
   Vehicle Detection
          |
          v
   Vehicle Tracking
          |
          +--------------------+
          |                    |
          v                    v
 Licence Plate            Traffic Flow
   Monitoring               Monitoring
          |                    |
          v                    v
Plate / Registration      Count / Density /
Anomaly Detection        Lane Movement
          |                    |
          +---------+----------+
                    |
                    v
          Analysis & Prediction
                    |
                    v
            AR / User Interface
```

---

## Project Status

**Current Stage:** Initial project planning and requirements analysis.

Project scope, detailed requirements, system architecture, technology choices, implementation priorities, and research directions will be refined as the project progresses.
