#!/usr/bin/env python3
#author: Oliver
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from snaak_vision.srv import GetDepthAtPoint
from snaak_vision.srv import GetXYZFromImage
import traceback
from visualization_msgs.msg import Marker
from geometry_msgs.msg import Point


from rclpy.qos import QoSProfile, DurabilityPolicy
from post_processing.image_utlis import ImageUtils
from tf2_msgs.msg import TFMessage
import numpy as np
from sensor_msgs.msg import CameraInfo
import cv2
from scipy.spatial.transform import Rotation as R

from cheese_segmentation.cheese_segment_generator import CheeseSegmentGenerator


#Make these config
HAM_BIN_ID = 1
CHEESE_BIN_ID = 2
BREAD_BIN_ID = 3


qos_profile = QoSProfile(depth=10, durability=DurabilityPolicy.TRANSIENT_LOCAL)


class VisionNode(Node):
    def __init__(self):
        super().__init__('vision_node')
        self.bridge = CvBridge()
        self.depth_image = None
        self.rgb_image = None

        # Start UNet 
        # self.cheese_unet = Ingredients_UNet(count=False, classes=["background","top_cheese","other_cheese"], model_path="logs/cheese/top_and_other/best_epoch_weights.pth") #TODO make these config 

        # post processing stuff
        self.img_utils = ImageUtils()

        # init cheese segmentation object
        self.cheese_segment_generator = CheeseSegmentGenerator()

        # Subscribe to depth image topic (adjust topic name as needed)
        self.depth_subscription = self.create_subscription(
            Image,
            '/camera/camera/depth/image_rect_raw',
            self.depth_callback,
            10)

        # Subscribe to rgb image topic
        self.RGB_subscription = self.create_subscription(
            Image, 
            '/camera/camera/color/image_rect_raw', 
            self.rgb_callback,
            10
        )

        # Create the service server
        self.service = self.create_service(GetDepthAtPoint, self.get_name()+'/get_depth_at_point', self.handle_get_depth)

        # Create the pickup point service server
        self.pickup_point_service = self.create_service(GetXYZFromImage, self.get_name()+'/get_pickup_point', self.handle_pickup_point)

        self.subscription_tf = self.create_subscription(TFMessage, '/tf', self.tf_listener_callback_tf, 10)
        self.subscription_intrinsics = self.create_subscription(CameraInfo, '/camera/camera/color/camera_info', self.camera_intrinsics_callback, 10) # TODO fix this
        self.subscription_tf_static = self.create_subscription(TFMessage,'/tf_static', self.tf_static_listener_callback_tf_static, qos_profile)

        self.marker_pub = self.create_publisher(Marker, 'visualization_marker', 10)
        
        self.transformations = {}
        self.K = np.eye(3)
        self.distortion_coefficients = np.zeros((1, 5))
        self.width = 0
        self.height = 0

        # for visualizing pickup point
        self.marker = Marker()
        self.marker.header.frame_id = 'camera_color_optical_frame'  # Frame of reference (e.g., base_link)
        self.marker.id = 0
        self.marker.type = Marker.SPHERE  # Marker type is a sphere
        self.marker.action = Marker.ADD
        self.marker.scale.x = 0.1  # Size of the sphere
        self.marker.scale.y = 0.1
        self.marker.scale.z = 0.1
        self.marker.color.a = 1.0  # Full opacity
        self.marker.color.r = 1.0  # Red color
        self.marker.color.g = 0.0
        self.marker.color.b = 0.0

    def tf_listener_callback_tf(self, msg):
        """ Handle incoming transform messages. """
        for transform in msg.transforms:
            if transform.child_frame_id and transform.header.frame_id:
                self.transformations[(transform.header.frame_id, transform.child_frame_id)] = transform

    def tf_static_listener_callback_tf_static(self, msg):
        """ Handle incoming transform messages. """
        for transform in msg.transforms:
            if transform.child_frame_id and transform.header.frame_id:
                self.transformations[(transform.header.frame_id, transform.child_frame_id)] = transform

    def depth_callback(self, msg):
        # Convert ROS Image message to OpenCV format
        self.depth_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
    
    def rgb_callback(self, msg):
        self.rgb_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')

    def camera_intrinsics_callback(self, msg):
        intrinsic_matrix = msg.k 
        self.K = np.array(intrinsic_matrix).reshape((3, 3))
        self.distortion_coefficients = np.array(msg.d)
        self.width = msg.width
        self.height = msg.height
    
    def quaternion_to_rotation_matrix(self, x, y, z, w):
        """ Convert a quaternion into a full three-dimensional rotation matrix. """
        return R.from_quat([x, y, z, w]).as_matrix()

    def handle_get_depth(self, request, response): #separate this from service callback stuff so the same function can be used for pickup point service
        if self.depth_image is None:
            self.get_logger().warn("Depth image not available yet!")
            response.depth = float('nan')
            return response

        try:
            # Ensure coordinates are within bounds of the image dimensions
            if request.x < 0 or request.x >= self.depth_image.shape[1] or \
               request.y < 0 or request.y >= self.depth_image.shape[0]:
                raise ValueError("Coordinates out of bounds")

            # Retrieve depth value at (x, y)
            response.depth = float(self.depth_image[request.y, request.x]) / 1000.0  # Convert mm to meters
            self.get_logger().info(f"Depth at ({request.x}, {request.y}): {response.depth} meters")
        except Exception as e:
            self.get_logger().error(f"Error retrieving depth: {e}")
            response.depth = float('nan')

        return response
    
    def dehomogenize(self, point_h):
        # Dehomogenize by dividing x, y, z by w
        x, y, z, w = point_h
        if w != 0:
            return (x / w, y / w, z / w)
        else:
            raise ValueError("Homogeneous coordinate w cannot be zero")

    def transform_location(self, x, y, depth):
        '''
        Modified Version of code from handeye_calibration_ros2
        '''

        # apply intrinsic transform:
        point_img_frame = np.array([x, y, 1])
        point_cam = np.linalg.inv(self.K)@point_img_frame
        point_cam = depth * point_cam  # Scale the normalized point by depth (Z)
        point_cam = np.concatenate([point_cam, np.array([1])])  # Homogenize the point

        # apply extrinsic transform
        T = np.eye(4)
        link_order = [
            ('panda_link0','panda_hand'), ('panda_hand','camera_color_optical_frame'),
        ]
        transform_matrices = {}

        for (frame_id, child_frame_id) in link_order:
            if (frame_id, child_frame_id) in self.transformations:
                trans = self.transformations[(frame_id, child_frame_id)].transform
                translation = [trans.translation.x, trans.translation.y, trans.translation.z]
                rotation = [trans.rotation.x, trans.rotation.y, trans.rotation.z, trans.rotation.w]
                T_local = np.eye(4)
                T_local[:3, :3] = self.quaternion_to_rotation_matrix(*rotation)
                T_local[:3, 3] = translation
                transform_matrices[(frame_id, child_frame_id)] = T_local
        

        T_link0_camera = transform_matrices[('panda_link0', 'panda_hand')]@transform_matrices[('panda_hand', 'camera_color_optical_frame')]
        
        self.get_logger().info("Got extrinsic transform, applying it to point...")

        # distorted_point = np.array([[x, y]], dtype=np.float32)
        # undistorted_point = cv2.undistortPoints(distorted_point, self.K, self.distortion_coefficients)
        # x_undistorted, y_undistorted = undistorted_point[0][0]
        # x = (x_undistorted - self.K[0, 2]) * depth / self.K[0, 0]
        # y = (y_undistorted - self.K[1, 2]) * depth / self.K[1, 1]

        # point_base_link = np.linalg.inv(T_link0_camera)@point_cam
        point_base_link = T_link0_camera@point_cam # why not inverse??????
    
        point_base_link = self.dehomogenize(point_base_link)
        
        return point_base_link

    def handle_pickup_point(self, request, response):
        bin_id = request.bin_id
        timestamp = request.timestamp #use this to sync
        image = self.rgb_image

        self.get_logger().info(f"{image.shape}")

        self.get_logger().info(f"Bin ID: {bin_id}")
        self.get_logger().info(f"Cheese Bin ID: {CHEESE_BIN_ID}")

        if bin_id == CHEESE_BIN_ID: 
            # Cheese
            try:
                # Get X, Y
                # UNET
                # mask = self.cheese_unet.detect_image(self.rgb_image)
                # top_layer_mask = self.cheese_unet.get_top_layer(mask, [250, 106, 77]) #TODO make color a config
                # binary_mask = Image.fromarray(self.img_utils.binarize_image(masked_img=np.array(top_layer_mask)))
                # binary_mask_edges, cont = self.img_utils.find_edges_in_binary_image(np.array(binary_mask))
                # (response.x, response.y) = self.img_utils.get_contour_center(cont)

                # SAM
                image = cv2.cvtColor(self.rgb_image, cv2.COLOR_RGB2BGR)
                mask = self.cheese_segment_generator.get_top_cheese_slice(image)

                self.get_logger().info(f"Max value in mask {np.max(mask)}")

                y_coords, x_coords = np.where(mask == 1)
                cam_x = int(np.mean(x_coords))
                cam_y = int(np.mean(y_coords))

                self.get_logger().info(f"Mid point {cam_x}, {cam_y}")
                
                cv2.circle(image, (cam_x, cam_y), 10, color=(255, 0, 0), thickness=-1)
                cv2.imwrite("/home/snaak/Documents/vision_ws/src/vision_node/src/cheese_segmentation/mask.jpg", mask * 255)
                cv2.imwrite("/home/snaak/Documents/vision_ws/src/vision_node/src/cheese_segmentation/img.jpg", image)

                # Middle of camera FOV
                # cam_x = 424
                # cam_y = 240

                # Get Z
                # Ensure coordinates are within bounds of the image dimensions
                if cam_x < 0 or cam_x >= self.depth_image.shape[1] or \
                cam_y < 0 or cam_y >= self.depth_image.shape[0]:
                    raise ValueError("Coordinates out of bounds")

                # Retrieve depth value at (x, y)
                cam_z = float(self.depth_image[int(cam_y/2.0), int(cam_x/2.0)]) / 1000.0  # Convert mm to meters

                # These adjustments need to be removed and the detection should be adjusted to account for the end effector size
                cam_z += 0.03 # now the end effector just touches the cheese, we need it to go a little lower to actually make a seal
                cam_x += 0.02 # the x is a little off - either the end effector is incorrectly described or the detection needs to be adjusted

                self.get_logger().info(f"Got Depth at {cam_x}, {cam_y}: {cam_z}")
                if cam_z == 0:
                    raise Exception("Invalid Z")
                # self.get_logger().info(f"Got pickup point {response.x}, {response.y} and depth {response.depth:.2f} in bin {bin_ID} at {timestamp}")

                self.get_logger().info("transforming coordinates...")

                self.get_logger().info(f"{cam_z}")    
                response_transformed = self.transform_location(cam_x, cam_y, cam_z)

                self.get_logger().info("got transform, applying it to point...")

                response.x = response_transformed[0]
                response.y = response_transformed[1]
                response.z = response_transformed[2]

                self.get_logger().info(f"Transformed coords: X: {response.x}, Y: {response.y}, Z:{response.z}")

                # publish point to topic
                self.marker.pose.position = Point(x=response.x, y=response.y, z=response.z)

                # Get the current time and set it in the header
                self.marker.header.stamp = self.get_clock().now().to_msg()

                # Publish the marker
                self.marker_pub.publish(self.marker)
                self.get_logger().info('Published point to RViz')


            except Exception as e:
                self.get_logger().error(f"Error while calculating pickup point: {e}")
                self.get_logger().error(traceback.print_exc())
                response.x = -1.0
                response.y = -1.0
                response.z = float('nan')
        
        return response

def main(args=None):
    rclpy.init(args=args)
    node = VisionNode()
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
