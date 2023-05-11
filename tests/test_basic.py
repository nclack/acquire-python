import json
import logging
import math
import sys
import time
from time import sleep
from typing import Any, Dict, List

import acquire
import dask.array as da
import numcodecs.blosc as blosc
import pytest
import tifffile
import zarr
from acquire.acquire import DeviceKind, DeviceState, Runtime, Trigger
from ome_zarr.io import parse_url
from ome_zarr.reader import Reader


@pytest.fixture(scope="module")
def runtime():
    runtime = acquire.Runtime()
    yield runtime


def test_set():
    t = Trigger()
    assert not t.enable
    t.enable = True
    assert t.enable


def test_list_devices(runtime: Runtime):
    dm = runtime.device_manager()
    for d in dm.devices():
        print(d.dict())


def test_set_camera_identifier(runtime: Runtime):
    dm = runtime.device_manager()

    p = runtime.get_configuration()
    assert (
        p.video[0].camera.identifier is not None
        and p.video[0].camera.identifier.kind == acquire.DeviceKind.NONE
    )
    p.video[0].camera.identifier = dm.select(
        acquire.DeviceKind.Camera, "simulated: radial sin"
    )
    assert p.video[0].camera.identifier is not None


@pytest.mark.parametrize(
    "input,expected",
    [
        (["does not exist 1", "does not exist 2", "does not exist 3"], None),
        (
            [
                "does not exist 1",
                "simulated: radial sin",
                "simulated: uniform random",
            ],
            "simulated: radial sin",
        ),
        (["simulated: radial sin"], "simulated: radial sin"),
        (["simulated.*sin"], "simulated: radial sin"),
        ([".*radial.*"], "simulated: radial sin"),
        ([], None),
    ],
)
def test_select_one_of(
    runtime: Runtime,
    input: List[str],
    expected: str,
):
    h = runtime.device_manager().select_one_of(DeviceKind.Camera, input)
    result = None if h is None else h.name
    assert result == expected


def test_select_empty_string(runtime: Runtime):
    assert runtime.device_manager().select(DeviceKind.Storage, "")


def test_zero_conf_start(runtime: Runtime):
    with pytest.raises(RuntimeError):
        runtime.start()


def test_repeat_acq(runtime: Runtime):
    p = acquire.setup(runtime, "simulated: radial sin", "Trash")
    assert p.video[0].camera.identifier is not None
    assert p.video[0].storage.identifier is not None
    assert p.video[0].storage.settings.filename == "out.tif"
    p.video[0].camera.settings.shape = (192, 108)
    p.video[0].max_frame_count = 10
    p = runtime.set_configuration(p)
    runtime.start()
    while True:
        if a := runtime.get_available_data(0):
            logging.info(f"Got {a.get_frame_count()}")
            break
    runtime.stop()
    assert runtime.get_available_data(0) is None
    # TODO: (nclack) assert 1 acquired frame. stop should block
    runtime.start()
    while True:
        if a := runtime.get_available_data(0):
            logging.info(f"Got {a.get_frame_count()}")
            break
    runtime.stop()
    assert runtime.get_available_data(0) is None
    # TODO: (nclack) assert 1 more acquired frame. stop cancels and waits.


def test_repeat_with_no_stop(runtime: Runtime):
    """Stop is required between starts. This tests that an exception is
    raised."""
    p = acquire.setup(runtime, "simulated: radial sin", "Trash")
    assert p.video[0].camera.identifier is not None
    assert p.video[0].storage.identifier is not None
    p.video[0].camera.settings.shape = (192, 108)
    p.video[0].max_frame_count = 11
    p = runtime.set_configuration(p)
    runtime.start()
    # wait for 1 frame
    while True:
        if a := runtime.get_available_data(0):
            logging.info(f"Got {a.get_frame_count()}")
            a = None
            break
    # acq is still on going here
    with pytest.raises(RuntimeError):
        logging.info("Next start should fail gracefully")
        runtime.start()
    runtime.stop()


def test_set_storage(runtime: Runtime):
    dm = runtime.device_manager()

    p = runtime.get_configuration()
    p.video[0].storage.identifier = None
    p = runtime.set_configuration(p)
    assert p.video[0].storage.identifier is not None
    assert p.video[0].storage.identifier.kind == acquire.DeviceKind.NONE
    p.video[0].storage.identifier = dm.select(
        acquire.DeviceKind.Storage, "Tiff"
    )
    assert p.video[0].storage.identifier is not None

    p.video[0].storage.settings.filename = "out.tif"
    assert p.video[0].storage.settings.filename == "out.tif"


def test_setup(runtime: Runtime):
    p = acquire.setup(runtime, "simulated.*empty", "Trash")
    assert p.video[0].camera.identifier is not None
    assert p.video[0].storage.identifier is not None
    assert p.video[0].storage.settings.filename == "out.tif"
    assert p.video[0].max_frame_count == 100
    p.video[0].camera.settings.shape = (192, 108)
    p = runtime.set_configuration(p)

    logging.info(f"max_frame_count: {p.video[0].max_frame_count}")

    runtime.start()

    nframes = 0
    t0 = time.time()

    def took_too_long():
        # Time limit the test
        return time.time() - t0 > 20.0

    while nframes < p.video[0].max_frame_count and not took_too_long():
        clock = time.time()
        if a := runtime.get_available_data(0):
            packet = a.get_frame_count()
            for f in a.frames():
                logging.info(
                    f"{f.data().shape} {f.data()[0][0][0][0]} {f.metadata()}"
                )
                del f  # <-- fails to get the last frames if this is held?
            del a  # <-- fails to get the last frames if this is held?
            nframes += packet
            logging.info(
                f"frame count: {nframes} - frames in packet: {packet}"
            )

        elapsed = time.time() - clock
        sleep(max(0, 0.1 - elapsed))
    logging.info("stopping")

    runtime.stop()
    if took_too_long():
        raise RuntimeError("Took too long")


def test_selection_is_consistent(runtime: Runtime):
    hcam1 = runtime.device_manager().select(DeviceKind.Camera)
    assert hcam1 is not None
    hcam2 = runtime.device_manager().select(DeviceKind.Camera, hcam1.name)
    assert hcam1 == hcam2


def test_change_filename(runtime: Runtime):
    dm = runtime.device_manager()
    p = runtime.get_configuration()
    p.video[0].camera.identifier = dm.select(DeviceKind.Camera, "simulated.*")
    p.video[0].storage.identifier = dm.select(DeviceKind.Storage, "Tiff")
    p.video[0].max_frame_count = 1

    names = [
        "out1.tif",
        "quite a bit longer.tif",
        "s.tif",
        "another long one ok it is really long this time.tif",
    ]
    for name in names:
        p.video[0].storage.settings.filename = name
        p = runtime.set_configuration(p)
        assert p.video[0].storage.settings.filename == name

        nframes = 0
        runtime.start()
        while nframes < p.video[0].max_frame_count:
            if packet := runtime.get_available_data(0):
                nframes += packet.get_frame_count()
                packet = None
        logging.info("Stopping")
        runtime.stop()


def test_write_external_metadata_to_tiff(
    runtime: Runtime, request: pytest.FixtureRequest
):
    dm = runtime.device_manager()
    p = runtime.get_configuration()
    p.video[0].camera.identifier = dm.select(
        DeviceKind.Camera, "simulated.*sin"
    )
    p.video[0].camera.settings.shape = (33, 47)
    p.video[0].storage.identifier = dm.select(DeviceKind.Storage, "Tiff")
    p.video[0].max_frame_count = 3
    p.video[0].storage.settings.filename = f"{request.node.name}.tif"
    metadata = {"hello": "world"}
    p.video[0].storage.settings.external_metadata_json = json.dumps(metadata)
    runtime.set_configuration(p)

    nframes = 0
    runtime.start()
    while nframes < p.video[0].max_frame_count:
        if packet := runtime.get_available_data(0):
            nframes += packet.get_frame_count()
            packet = None
    runtime.stop()

    # Check that the written tif has the expected structure
    with tifffile.TiffFile(p.video[0].storage.settings.filename) as f:

        def meta(iframe: int) -> Dict[Any, Any]:
            return json.loads(f.pages[iframe].tags["ImageDescription"].value)

        # first frame should have metadata
        assert meta(0)["metadata"] == metadata
        assert meta(0)["frame_id"] == 0

        # remaining frames should not, but should have e.g. frame id
        for i in range(1, p.video[0].max_frame_count):
            assert "metadata" not in meta(i).keys()
            assert meta(i)["frame_id"] == i


@pytest.mark.skipif(sys.platform == "darwin", reason="illegal instruction")
def test_write_external_metadata_to_zarr(
    runtime: Runtime, request: pytest.FixtureRequest
):
    dm = runtime.device_manager()
    p = runtime.get_configuration()
    p.video[0].camera.identifier = dm.select(
        DeviceKind.Camera, "simulated.*sin.*"
    )
    p.video[0].camera.settings.shape = (33, 47)
    p.video[0].storage.identifier = dm.select(DeviceKind.Storage, "Zarr")
    p.video[0].max_frame_count = 4
    p.video[0].storage.settings.filename = f"{request.node.name}.zarr"
    metadata = {"hello": "world"}
    p.video[0].storage.settings.external_metadata_json = json.dumps(metadata)
    p.video[0].storage.settings.pixel_scale_um = (0.5, 4)

    p = runtime.set_configuration(p)

    nframes = 0
    runtime.start()
    while nframes < p.video[0].max_frame_count:
        if packet := runtime.get_available_data(0):
            nframes += packet.get_frame_count()
            packet = None
    runtime.stop()

    assert p.video[0].storage.settings.filename
    store = parse_url(p.video[0].storage.settings.filename)
    assert store
    reader = Reader(store)
    nodes = list(reader())

    # ome-ngff supports multiple images, in separate directories but we only
    # wrote one.
    multi_scale_image_node = nodes[0]

    # ome-ngff always stores multi-scale images, but we only have a single
    # scale/level.
    image_data = multi_scale_image_node.data[0]
    assert image_data.shape == (
        p.video[0].max_frame_count,
        1,
        p.video[0].camera.settings.shape[1],
        p.video[0].camera.settings.shape[0],
    )

    multi_scale_image_metadata = multi_scale_image_node.metadata

    axes = multi_scale_image_metadata["axes"]
    axis_names = tuple(a["name"] for a in axes)
    assert axis_names == ("t", "c", "y", "x")

    axis_types = tuple(a["type"] for a in axes)
    assert axis_types == ("time", "channel", "space", "space")

    axis_units = tuple(a.get("unit") for a in axes)
    assert axis_units == (None, None, "micrometer", "micrometer")

    # We only have one multi-scale level and one transform.
    transform = multi_scale_image_metadata["coordinateTransformations"][0][0]
    pixel_scale_um = tuple(
        transform["scale"][axis_names.index(axis)] for axis in ("x", "y")
    )
    assert pixel_scale_um == p.video[0].storage.settings.pixel_scale_um

    # ome-zarr only reads attributes it recognizes, so use a plain zarr reader
    # to read external metadata instead.
    group = zarr.open(p.video[0].storage.settings.filename)
    assert group["0"].attrs.asdict() == metadata


@pytest.mark.skipif(sys.platform == "darwin", reason="illegal instruction")
@pytest.mark.parametrize(
    ("storage_kind", "compressor_name", "clevel", "shuffle"),
    [
        ("ZarrBlosc1ZstdByteShuffle", "zstd", 1, blosc.SHUFFLE),
        ("ZarrBlosc1Lz4ByteShuffle", "lz4", 1, blosc.SHUFFLE),
    ],
)
def test_write_compressed_zarr(
    runtime: Runtime,
    request: pytest.FixtureRequest,
    storage_kind: str,
    compressor_name: str,
    clevel: int,
    shuffle: int,
):
    filename = f"{request.node.name}.zarr"
    filename = filename.replace("[", "_").replace("]", "_")

    dm = runtime.device_manager()
    p = runtime.get_configuration()
    p.video[0].camera.identifier = dm.select(
        DeviceKind.Camera, "simulated.*sin.*"
    )
    p.video[0].camera.settings.shape = (64, 48)
    p.video[0].storage.identifier = dm.select(
        DeviceKind.Storage, "ZarrBlosc1ZstdByteShuffle"
    )
    p.video[0].camera.settings.exposure_time_us = 1e4
    p.video[0].storage.identifier = dm.select(DeviceKind.Storage, storage_kind)
    p.video[0].max_frame_count = 70
    p.video[0].storage.settings.filename = filename
    metadata = {"foo": "bar"}
    p.video[0].storage.settings.external_metadata_json = json.dumps(metadata)
    runtime.set_configuration(p)

    runtime.start()
    runtime.stop()

    # load from Zarr
    group = zarr.open(p.video[0].storage.settings.filename)
    data = group["0"]

    assert data.compressor.cname == compressor_name
    assert data.compressor.clevel == clevel
    assert data.compressor.shuffle == shuffle

    assert data.shape == (
        p.video[0].max_frame_count,
        1,
        p.video[0].camera.settings.shape[1],
        p.video[0].camera.settings.shape[0],
    )
    assert data.attrs.asdict() == metadata

    # load from Dask
    data = da.from_zarr(p.video[0].storage.settings.filename, component="0")
    assert data.shape == (
        p.video[0].max_frame_count,
        1,
        p.video[0].camera.settings.shape[1],
        p.video[0].camera.settings.shape[0],
    )


@pytest.mark.skipif(sys.platform == "darwin", reason="illegal instruction")
@pytest.mark.parametrize(
    ("frame_x", "frame_y", "frames_per_chunk", "compressed"),
    [(64, 48, 25, False), (96, 72, 66, False), (1920, 1080, 32, True)],
)
def test_write_raw_zarr_with_variable_chunking(
    runtime: Runtime,
    request: pytest.FixtureRequest,
    frame_x: int,
    frame_y: int,
    frames_per_chunk: int,
    compressed: bool,
):
    max_frames = 100

    dm = runtime.device_manager()

    p = runtime.get_configuration()
    p.video[0].camera.identifier = dm.select(
        DeviceKind.Camera, "simulated.*empty.*"
    )
    p.video[0].camera.settings.shape = (frame_x, frame_y)
    p.video[0].camera.settings.exposure_time_us = 1e4
    p.video[0].storage.identifier = dm.select(
        DeviceKind.Storage,
        "ZarrBlosc1ZstdByteShuffle" if compressed else "Zarr",
    )
    p.video[0].storage.settings.filename = f"{request.node.name}.zarr"
    p.video[0].max_frame_count = max_frames
    p.video[0].storage.settings.bytes_per_chunk = (
        frame_x * frame_y * frames_per_chunk
    )
    runtime.set_configuration(p)

    n_chunks_expected = int(math.ceil(max_frames / frames_per_chunk))

    runtime.start()
    runtime.stop()

    group = zarr.open(p.video[0].storage.settings.filename)
    data = group["0"]

    assert data.shape == (
        p.video[0].max_frame_count,
        1,
        p.video[0].camera.settings.shape[1],
        p.video[0].camera.settings.shape[0],
    )
    assert data.nchunks == n_chunks_expected


@pytest.mark.skip(
    reason="Runs into memory limitations on github ci."
    + " See https://github.com/acquire-project/cpx/issues/147"
)
def test_two_video_streams(runtime: Runtime):
    dm = runtime.device_manager()
    p = runtime.get_configuration()

    p.video[0].camera.identifier = dm.select(
        DeviceKind.Camera, "simulated.*random.*"
    )
    p.video[0].storage.identifier = dm.select(DeviceKind.Storage, "Trash")
    p.video[0].camera.settings.binning = 1
    p.video[0].camera.settings.shape = (64, 64)
    p.video[0].camera.settings.pixel_type = acquire.SampleType.U8
    p.video[0].max_frame_count = 90
    p.video[0].frame_average_count = 0  # disables

    p.video[1].camera.identifier = dm.select(
        DeviceKind.Camera, "simulated.*empty.*"
    )
    p.video[1].storage.identifier = dm.select(DeviceKind.Storage, "Trash")
    p.video[1].camera.settings.binning = 1
    p.video[1].camera.settings.shape = (64, 64)
    p.video[1].camera.settings.pixel_type = acquire.SampleType.U8
    p.video[1].max_frame_count = 71
    p.video[1].frame_average_count = 0  # disables

    p = runtime.set_configuration(p)

    nframes = [0, 0]

    def is_not_done() -> bool:
        return runtime.get_state() == DeviceState.Running and (
            (nframes[0] < p.video[0].max_frame_count)
            or (nframes[1] < p.video[1].max_frame_count)
        )

    runtime.start()

    stream_id = 0
    while is_not_done():
        if nframes[stream_id] < p.video[stream_id].max_frame_count:
            if packet := runtime.get_available_data(stream_id):
                n = packet.get_frame_count()
                for i, frame in enumerate(packet.frames()):
                    expected_frame_id = nframes[stream_id] + i
                    assert frame.metadata().frame_id == expected_frame_id, (
                        "frame id's didn't match "
                        + f"({frame.metadata().frame_id}!={expected_frame_id})"
                        + f" [stream {stream_id} nframes {nframes}]"
                    )
                    del frame
                del packet
                nframes[stream_id] += n
                logging.debug(f"NFRAMES {nframes}")

        stream_id = (stream_id + 1) % 2
    logging.info("Stopping")
    runtime.stop()
    assert nframes[0] == p.video[0].max_frame_count
    assert nframes[1] == p.video[1].max_frame_count


def test_abort(runtime: Runtime):
    dm = runtime.device_manager()
    p = runtime.get_configuration()
    p.video[0].camera.identifier = dm.select(
        DeviceKind.Camera, "simulated.*sin"
    )
    p.video[0].camera.settings.shape = (24, 93)
    p.video[0].storage.identifier = dm.select(DeviceKind.Storage, "Trash")
    p.video[0].max_frame_count = 2**30
    runtime.set_configuration(p)

    nframes = 0
    runtime.start()
    sleep(0.05)
    logging.info("Aborting")
    runtime.abort()

    while packet := runtime.get_available_data(0):
        nframes += packet.get_frame_count()

    del packet

    logging.debug(
        f"Frames expected: {p.video[0].max_frame_count}, actual: {nframes}"
    )
    assert nframes < p.video[0].max_frame_count


# FIXME: (nclack) awkwardness around references  (available frames, f)

# NOTES:
#
# With pytest, use `--log-cli-level=0` to see the lowest level logs.
