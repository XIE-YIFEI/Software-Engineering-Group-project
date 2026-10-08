# Vehicle Detection and Multi-Object Tracking
**Preliminary Literature Investigation**

**Module:** COMP2019 – Software Engineering Group Project  
**Group:** Group 2  
**Research Area:** Vehicle Detection and Multi-Object Tracking  
**Stage:** Preliminary Research – Autumn Semester, Week 2

---

## 1. Role in Our Project

Vehicle detection and multi-object tracking (MOT) are important components of our proposed intelligent traffic monitoring system. Based on the current project description, the system is expected to support functions such as real-time vehicle counting, traffic density estimation, lane movement monitoring, and the prevention of duplicate vehicle counts.

Although vehicle detection and tracking are closely related, they perform different tasks.

**Vehicle detection** identifies vehicles within individual video frames, typically producing bounding boxes, class labels and confidence scores. Deep-learning-based detectors, such as the YOLO family, are commonly used for this purpose.

**Multi-Object Tracking (MOT)** attempts to associate detections across consecutive frames, allowing the system to maintain a consistent identity for each vehicle.

For example, a vehicle appearing in ten consecutive frames should ideally retain the same tracking ID rather than being treated as ten different vehicles. This is particularly important for vehicle counting, where repeated detections could otherwise lead to incorrect results.

A possible processing pipeline for our project is:

```text
Traffic Video
      |
      v
Vehicle Detection
      |
      v
Bounding Boxes and Confidence Scores
      |
      v
Multi-Object Tracking
      |
      v
Persistent Vehicle IDs and Trajectories
      |
      v
Vehicle Counting / Traffic Flow Analysis
```

Therefore, reliable vehicle tracking could provide a foundation for several other functions within our traffic monitoring system.

## 2. General Multi-Object Tracking Pipeline

A commonly used approach to MOT is **tracking-by-detection**, where an object detector first identifies objects in each frame, and a tracking algorithm subsequently associates detections across frames.

The systematic literature review on MOT in traffic environments identifies object detection, object association and trajectory management as important elements of tracking systems [1].

The general process can be divided into four stages.

### 2.1 Object Detection

An object detector processes each video frame and identifies vehicles, usually providing bounding boxes, class labels and confidence scores.

For example, a detector may return:

- Vehicle class: Car
- Confidence score: 0.94
- Bounding box: (x1, y1, x2, y2)

The investigated literature includes implementations combining YOLO-based detection with DeepSORT [2], as well as RT-DETR with ByteTrack for vehicle tracking and counting [4].

These studies suggest that detection and tracking can be implemented as connected components, allowing different detectors and trackers to be investigated separately.

### 2.2 Motion Prediction

Tracking algorithms can estimate the expected position of an existing object in the next frame based on its previous movement.

Both DeepSORT and ByteTrack use Kalman filtering for motion prediction. This helps estimate where an existing vehicle is likely to appear and provides information for subsequent matching.

### 2.3 Data Association

Data association determines which detections in the current frame correspond to previously tracked objects.

This becomes challenging when multiple vehicles appear close together, have similar appearances, or partially occlude one another.

The investigated DeepSORT and ByteTrack approaches both use the Hungarian algorithm for matching, although the information used to construct the matching costs differs.

### 2.4 Track Management

After data association, the tracker updates existing tracks, creates new tracks when necessary and manages tracks that temporarily disappear.

Ideally, the same vehicle should maintain a consistent ID across consecutive frames:

```text
Frame 1: Vehicle ID 15
Frame 2: Vehicle ID 15
Frame 3: Vehicle ID 15
```

However, tracking IDs are not guaranteed to remain correct. Missed detections or incorrect associations may cause identity switches or fragmented trajectories.

For our project, these problems could affect vehicle counting accuracy and traffic flow analysis. Therefore, maintaining tracking continuity is an important consideration.

---

## 3. DeepSORT

### 3.1 Basic Mechanism

DeepSORT is a multi-object tracking algorithm that combines motion information with appearance information to associate objects across video frames.

Based on the investigated literature, its main components include:

1. **Object Detection:** A detector identifies objects and produces bounding boxes.
2. **Motion Prediction:** A Kalman Filter estimates the expected positions of existing tracks.
3. **Appearance Feature Extraction:** A neural network extracts visual features to help distinguish objects.
4. **Data Association:** Motion-related distance and appearance similarity are used to match detections with existing tracks.
5. **Track Management:** Existing tracks are updated, while new or unmatched objects are handled accordingly.

DeepSORT commonly uses Mahalanobis distance for motion-related matching and cosine distance for appearance similarity. The Hungarian algorithm is then used as part of the assignment process [2, 3].

Its main distinguishing feature is the use of appearance information in addition to motion prediction.

### 3.2 Advantages

A potential advantage of DeepSORT is its ability to use both motion and visual appearance when maintaining object identities.

In traffic monitoring, this could be useful when vehicles temporarily overlap or become partially occluded. Appearance features may help distinguish vehicles even when their predicted positions are close together.

The investigated vehicle-tracking studies demonstrate the application of DeepSORT to traffic scenarios [2, 3]. Research on improved DeepSORT also suggests that enhancing appearance feature extraction and association can improve identity consistency.

### 3.3 Limitations and Critical Analysis

Although DeepSORT incorporates appearance information, it does not completely eliminate identity switches.

Traffic environments can contain many vehicles with similar appearances, such as cars of the same colour and model. This may reduce the effectiveness of appearance-based matching.

Furthermore, extracting appearance features introduces additional processing requirements. This could affect real-time performance, particularly if the system operates on limited hardware.

Severe occlusion, lighting changes and missed detections may also cause tracking failures.

For our project, these limitations suggest that appearance-based tracking may be beneficial, but its additional complexity should be justified by the actual requirements of the traffic monitoring system.

### 3.4 Relevance to Our Project

DeepSORT could be considered if maintaining vehicle identities becomes difficult using motion-based association alone.

Its appearance features may be particularly useful in crowded traffic scenes where vehicles frequently overlap.

However, the importance of Re-Identification (Re-ID) will depend on the expected camera setup, video quality and traffic conditions.

Therefore, DeepSORT remains a potential tracking solution, but its suitability should be evaluated against the project's requirements rather than assumed from existing studies.

---

## 4. ByteTrack

### 4.1 Basic Mechanism

ByteTrack is another tracking-by-detection algorithm. Its main distinguishing feature is the use of both high-confidence and low-confidence detections during data association.

In many detection-based systems, bounding boxes below a confidence threshold may be discarded. However, a genuine vehicle may receive a lower confidence score because of partial occlusion, motion blur or other challenging conditions.

ByteTrack addresses this issue by performing association in two main stages [5].

1. **First Association:** High-confidence detections are matched with existing tracks.
2. **Second Association:** Remaining unmatched tracks are compared with low-confidence detections.
3. **Track Management:** Matched tracks are updated, and unmatched detections and tracks are handled according to the track management rules.

This approach attempts to recover valid objects from lower-confidence detections instead of immediately discarding them.

A simplified representation is:

```text
Vehicle Detections
        |
        v
Confidence-Based Separation
        |
        +----------------------+
        |                      |
        v                      v
High-Confidence          Low-Confidence
Detections               Detections
        |
        v
First Association
        |
        v
Unmatched Tracks
        |
        v
Second Association <---- Low-Confidence Detections
        |
        v
Updated Vehicle Tracks
```

### 4.2 Advantages

ByteTrack's association strategy may be useful in traffic monitoring, where vehicles can become partially occluded by other vehicles.

Instead of immediately losing a track when detection confidence decreases, ByteTrack attempts to associate lower-confidence detections with existing trajectories.

This could help maintain consistent vehicle IDs and reduce tracking fragmentation.

The investigated RT-DETR and ByteTrack framework demonstrates how ByteTrack can be integrated with vehicle tracking and counting [4].

This makes ByteTrack particularly relevant to our project's intended vehicle counting functionality.

### 4.3 Limitations and Critical Analysis

Although ByteTrack attempts to recover low-confidence detections, this approach does not guarantee correct associations.

Low-confidence detections may include false positives. Therefore, the association process must distinguish genuine objects from incorrect detections.

Furthermore, the original ByteTrack approach does not rely on a dedicated appearance-based Re-ID model in the same way as standard DeepSORT. This may create difficulties when motion and spatial information are insufficient to distinguish vehicles.

The investigated improved ByteTrack study introduces appearance information, DIoU and motion compensation to address tracking limitations [5].

This suggests that ByteTrack can still experience identity association problems, particularly under challenging conditions.

For our project, its effectiveness would need to be tested using representative traffic footage before drawing conclusions about its counting accuracy or real-time performance.

### 4.4 Relevance to Our Project

ByteTrack appears particularly relevant to vehicle tracking and counting.

By maintaining vehicle IDs and trajectories, the system could identify when a tracked vehicle crosses a predefined counting line.

For example, a vehicle could be counted once when its tracked trajectory crosses the line in a specified direction, rather than being counted in every frame.

However, persistent IDs alone do not guarantee accurate counting. Identity switches, fragmented tracks and incorrect line-crossing detection could still produce counting errors.

Therefore, the counting logic would need to be designed and evaluated alongside the tracking algorithm.

ByteTrack may be a reasonable candidate for an initial prototype, but this remains a preliminary suggestion rather than a final technical decision.

---

## 5. Preliminary Comparison: DeepSORT vs ByteTrack

Based on the literature investigated, DeepSORT and ByteTrack are both potential solutions for multi-object vehicle tracking, but they use different strategies to support data association.

| Aspect | DeepSORT | ByteTrack |
|---|---|---|
| Main approach | Tracking-by-detection | Tracking-by-detection |
| Motion prediction | Kalman Filter | Kalman Filter |
| Key feature | Motion and appearance information | High- and low-confidence detection association |
| Appearance Re-ID | Included in standard DeepSORT | Not central to the original ByteTrack approach |
| Occlusion handling | Appearance features may help maintain identity | Low-confidence association may help maintain tracks |
| Processing considerations | Additional appearance feature extraction | Association without a dedicated Re-ID model |
| Potential limitations | Similar-looking vehicles, ID switches, additional processing | Incorrect associations, ID switches, dependence on detection quality |
| Potential project benefit | Identity tracking in crowded traffic | Tracking continuity and vehicle counting |

### 5.1 Implications for Our Project

The main difference between these approaches is not simply which one achieves higher tracking accuracy in published experiments, but how each addresses tracking difficulties.

DeepSORT may be useful when visual appearance provides valuable information for distinguishing vehicles.

ByteTrack may be useful when vehicles remain spatially predictable but their detection confidence temporarily decreases.

However, these are potential advantages rather than guaranteed outcomes.

Published performance results cannot be compared directly unless the evaluation conditions are sufficiently similar, including datasets, object detectors, hardware and evaluation procedures.

Consequently, the current literature does not provide enough evidence to conclude that either tracker will necessarily perform better in our proposed system.

---

## 6. Preliminary Findings for Our Project

### 6.1 Potential System Architecture

The investigation suggests that a modular tracking-by-detection architecture could be suitable for our project.

A possible architecture is:

```text
Traffic Camera / Video
          |
          v
    Object Detector
          |
          v
Vehicle Bounding Boxes
          |
          v
 DeepSORT / ByteTrack
          |
          v
Persistent IDs and Trajectories
          |
          v
  +-------+--------+
  |                |
  v                v
Vehicle         Traffic Flow
Counting         Analysis
```

This architecture would allow the detector and tracker to be evaluated or replaced separately, depending on the project's requirements.

It may also support integration with other components of the traffic monitoring system.

### 6.2 Initial Technical Considerations

Based on the current investigation, ByteTrack could be considered as an initial tracking prototype because of its relatively straightforward association strategy and existing applications in vehicle tracking and counting.

However, DeepSORT remains a relevant alternative, particularly if appearance-based matching proves useful for the expected traffic footage.

At this stage, the group should avoid making a final decision before clarifying the project requirements and discussing the options with the supervisor.

### 6.3 Evaluation Considerations

If a tracking prototype is developed, several evaluation measures could be considered.

| Evaluation Measure | Purpose | Relevance to Our Project |
|---|---|---|
| ID Switches (IDSW) | Measures changes in assigned object identities | Helps assess identity consistency |
| IDF1 | Measures identity association performance | Relevant to maintaining vehicle IDs |
| MOTA | Summarises tracking errors involving false positives, missed detections and ID switches | Provides an overall tracking measure |
| FPS | Measures processing speed | Relevant to real-time performance |
| Vehicle Counting Error | Compares estimated counts with ground-truth counts | Directly evaluates a key project requirement |

Tracking metrics such as MOTA and IDF1 generally require appropriately annotated ground-truth data. Therefore, their use will depend on the available datasets and the scope of the project.

For vehicle counting, a simpler evaluation could involve comparing the system's output against manually verified counts from selected traffic videos.

A useful initial test could involve videos with different traffic conditions, such as low traffic density, heavy traffic and partial vehicle occlusion.

These are proposed evaluation considerations, not experiments that have already been conducted.

### 6.4 Technical Risks and Uncertainties

Several issues remain uncertain at this stage:

- **Camera setup:** Fixed and moving cameras may create different tracking challenges.
- **Occlusion:** Heavy traffic could increase tracking fragmentation and identity switches.
- **Detection quality:** Missed or incorrect vehicle detections could affect subsequent tracking.
- **Hardware constraints:** Real-time performance may depend on the available processing resources.
- **Counting reliability:** Incorrect IDs or trajectories could lead to missed or duplicate counts.
- **Data and privacy:** The use of traffic surveillance footage may require consideration of data sources, permissions and privacy requirements.

These uncertainties should be discussed with the supervisor before finalising the implementation approach.

### 6.5 Suggested Next Steps

Following the first supervisor meeting, the group could consider the following steps:

1. Confirm the expected traffic video sources and camera setup.
2. Clarify the tracking and vehicle counting requirements.
3. Select an initial detector and tracker for a simple prototype.
4. Test the prototype on a small amount of representative traffic footage.
5. Identify tracking failures and counting errors.
6. Decide whether further comparison or improvement is necessary.

The objective of the initial prototype should be to investigate feasibility and technical challenges rather than immediately develop a complete system.

---

## 7. Questions for Supervisor

Based on this preliminary investigation, the following questions could help clarify the technical direction of the project.

**Q1. Tracking Algorithm Selection**

Should we initially implement one tracking algorithm, such as ByteTrack, or compare DeepSORT and ByteTrack experimentally before selecting an approach?

**Q2. Camera Setup and Video Sources**

What type of traffic footage should we expect to use? Will the system primarily process fixed CCTV footage, and will sample datasets or videos be provided?

**Q3. Tracking Requirements**

Is maintaining vehicle identities within a single camera sufficient, or will more advanced Re-Identification or multi-camera tracking be required?

**Q4. Performance and Evaluation**

What level of real-time performance and vehicle counting accuracy is expected? Should we evaluate standard MOT metrics, or focus mainly on the functionality and reliability of the integrated system?

**Q5. Expected Technical Scope**

Would integrating existing detection and tracking algorithms be sufficient for this project, or is the group expected to modify or improve the underlying algorithms?

---

## 8. References

The following papers were investigated during this preliminary research. Full bibliographic details should be verified and formatted consistently before the document is submitted or incorporated into the interim report.

[1] *Multi-object Tracking in Traffic Environments: A Systematic Literature Review*. Neurocomputing, 2022.

[2] *Realtime Vehicle Tracking Method Based on YOLOv5 + DeepSORT*.

[3] *Multi-Target Vehicle Tracking Algorithm Based on Improved DeepSORT*.

[4] *Multi-Vehicle Tracking and Counting Framework in Average Daily Traffic Survey Using RT-DETR and ByteTrack*.

[5] *Multi-Object Tracking Algorithm Based on Improved ByteTrack*. IEEE ECNCT, 2024.

[6] *YOLOv8-DeepSORT: A High-Performance Framework for Real-Time Multi-Object Tracking with Attention and Adaptive Optimization*, 2025.

**Note:** This document represents a preliminary investigation based on selected literature rather than a systematic review. The findings and technical suggestions are intended to support initial project planning and discussion with the supervisor. Final implementation decisions will depend on confirmed requirements and subsequent prototype evaluation.