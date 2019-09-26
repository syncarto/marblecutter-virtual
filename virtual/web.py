# coding=utf-8
from __future__ import absolute_import

import logging
import json

from cachetools.func import lru_cache
from flask import Flask, Markup, jsonify, redirect, render_template, request
from flask_cors import CORS
from marblecutter import NoCatalogAvailable, tiling
from marblecutter.formats.optimal import Optimal
from marblecutter.transformations import Image
from marblecutter.web import bp, url_for
from marblecutter.utils import Source
import mercantile
import requests
from werkzeug.datastructures import ImmutableMultiDict

try:
    from urllib.parse import urlparse, urlencode
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError
except ImportError:
    from urlparse import urlparse
    from urllib import urlencode
    from urllib2 import urlopen, Request, HTTPError

from .catalogs import VirtualCatalog

LOG = logging.getLogger(__name__)

IMAGE_TRANSFORMATION = Image()
IMAGE_FORMAT = Optimal()

app = Flask("marblecutter-virtual")
app.register_blueprint(bp)
app.url_map.strict_slashes = False
CORS(app, send_wildcard=True)


@lru_cache()
def make_catalog(args):
    if args.get("url", "") == "":
        raise NoCatalogAvailable()

    try:
        return VirtualCatalog(
            args["url"],
            rgb=args.get("rgb"),
            nodata=args.get("nodata"),
            linear_stretch=args.get("linearStretch"),
            resample=args.get("resample"),
        )
    except Exception as e:
        LOG.exception(e)
        raise NoCatalogAvailable()


@app.route("/")
def index():
    return (render_template("index.html"), 200, {"Content-Type": "text/html"})


@app.route("/tiles/")
def meta():
    catalog = make_catalog(request.args)

    meta = {
        "bounds": catalog.bounds,
        "center": catalog.center,
        "maxzoom": catalog.maxzoom,
        "minzoom": catalog.minzoom,
        "name": catalog.name,
        "tilejson": "2.1.0",
        "tiles": [
            "{}{{z}}/{{x}}/{{y}}?{}".format(
                url_for("meta", _external=True, _scheme=""), urlencode(request.args)
            )
        ],
    }

    return jsonify(meta)


@app.route("/bounds/")
def bounds():
    catalog = make_catalog(request.args)

    return jsonify({"url": catalog.uri, "bounds": catalog.bounds})


@app.route("/preview")
def preview():
    try:
        # initialize the catalog so this route will fail if the source doesn't exist
        make_catalog(request.args)
    except Exception:
        return redirect(url_for("index"), code=303)

    return (
        render_template(
            "preview.html",
            tilejson_url=Markup(
                url_for("meta", _external=True, _scheme="", **request.args)
            ),
            source_url=request.args["url"],
        ),
        200,
        {"Content-Type": "text/html"},
    )


@app.route("/stac/<int:z>/<int:x>/<int:y>")
@app.route("/stac/<int:z>/<int:x>/<int:y>@<int:scale>x")
def render_png_from_stac_catalog(z, x, y, scale=1):
    def bbox_overlaps(bbox1, bbox2):
        # https://stackoverflow.com/questions/306316/determine-if-two-rectangles-overlap-each-other
        # assume STAC ordering
        west1, south1, east1, north1 = bbox1
        west2, south2, east2, north2 = bbox2
        return west1 < east2 and east1 > west2 and north1 > south2 and south1 < north2

    # example:
    # https://4reb3lh9m6.execute-api.us-west-2.amazonaws.com/stage/stac/search
    # test tile:
    # http://localhost:8000/stac/16/16476/24074@2x?url=https%3A%2F%2F4reb3lh9m6.execute-api.us-west-2.amazonaws.com%2Fstage%2Fstac%2Fsearch
    # compare result to single geotiff version:
    # http://localhost:8000/tiles/16/16476/24074@2x?url=https%3A%2F%2Fs3-us-west-2.amazonaws.com%2Fsyncarto-data-test%2Foutput%2F060801NE_COG.TIF
    stac_catalog_url = request.args["url"]

    tile = mercantile.Tile(x, y, z)
    bounds = mercantile.bounds(x, y, z)

    # per https://github.com/radiantearth/stac-spec/blob/master/api-spec/filters.md
    tile_bbox = [bounds.west, bounds.south, bounds.east, bounds.north]

    params = {
                'bbox': str(tile_bbox).replace(' ', ''),
                'limit': 500,
            }

    # Handle a couple special query parameters to query by property
    # Could make this more general but it would require e.g. passing json properties list
    query_filter = dict()
    collection = request.args.get('collection', None)
    if collection:
        query_filter['collection'] = {
                        "eq": collection
                    }
    constellation = request.args.get('constellation', None)
    if constellation is None:
        constellation = request.args.get('eo:constellation', None)
    if constellation:
        query_filter['eo:constellation'] = {
                        "eq": constellation
                    }

    if len(query_filter.keys()) > 0:
        params['query'] = json.dumps(query_filter)

    time = request.args.get('time', None)
    if time:
        params['time'] = time

    footprint_props = request.args.get('footprintProperties', None)
    # test url:
    # http://localhost:8000/stac/16/16642/23807?url=https%3A%2F%2F4reb3lh9m6.execute-api.us-west-2.amazonaws.com%2Fstage%2Fstac%2Fsearch&constellation=NAIP&footprintProperties=naip%3Aquarter_quad_id&time=2010-01-01T00%3A00%3A00Z%2F2018-01-01T00%3A00%3A00Z
    if footprint_props:
        latest_by_footprint_filter = {
                  'footprintProperties': footprint_props.split(',')
                }
        params['latestFilter'] = json.dumps(latest_by_footprint_filter)

    # pass through auth header from client to sat-api so it can do org auth
    headers = {
                'Authorization': request.headers.get('Authorization', '')
            }

    response = requests.get(stac_catalog_url, params=params, headers=headers)
    LOG.info('stac url: {}'.format(response.url))
    assert response.status_code == 200
    features = response.json()['features']
    LOG.info('{} number of features: {}'.format(response.url, len(features)))

    image_urls = []
    for feature in features:
        feature_bbox = feature['bbox']
        if not bbox_overlaps(feature_bbox, tile_bbox):
            # filter to bbox's that actually overlap; sat-api elasticsearch
            # precision not good enough for our <1km tiles
            continue

        # TODO more general way to determine which asset to use?
        try:
            # Works for NAIP and custom imagery
            image_urls.append(feature['assets']['visual']['href'])
        except KeyError:
            # works for landsat
            image_urls.append(feature['assets']['B2']['href'])

    LOG.info('features left after bbox overlap filter: {}'.format(len(image_urls)))

    sources = []
    if len(image_urls) > 0:
        # make a catalog from first image for getting name and resolution
        # Need to emulate same type of arg as flask request.args (specifically be immutable)
        # (Making catalog for every uri is SLOW)
        test_catalog = make_catalog(ImmutableMultiDict([('url', image_urls[0])]))

        for i, image_url in enumerate(image_urls):
            source = Source(
                url=image_url,
                name=test_catalog._name + str(i),
                resolution=test_catalog._resolution,
                recipes={"imagery": True},
            )
            sources.append(source)

    headers, data = tiling.render_tile_from_sources(
        tile,
        sources,
        format=IMAGE_FORMAT,
        transformation=IMAGE_TRANSFORMATION,
        scale=scale,
    )

    # ???
    # headers.update(catalog.headers)

    return data, 200, headers


@app.route("/tiles/<int:z>/<int:x>/<int:y>")
@app.route("/tiles/<int:z>/<int:x>/<int:y>@<int:scale>x")
def render_png(z, x, y, scale=1):
    catalog = make_catalog(request.args)
    tile = mercantile.Tile(x, y, z)

    headers, data = tiling.render_tile(
        tile,
        catalog,
        format=IMAGE_FORMAT,
        transformation=IMAGE_TRANSFORMATION,
        scale=scale,
    )

    headers.update(catalog.headers)

    return data, 200, headers
