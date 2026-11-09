"""Pixel operations on OpenImageIO buffers, behind ``GriptapeImagePipeline`` steps and encoding.

Buffers hold unassociated alpha, last: gray (Y), gray and alpha (Y, A), RGB, or RGBA. Operations
that filter or composite premultiply first, so the color of transparent pixels doesn't bleed into
their neighbors, and return a buffer of the source's pixel type.
"""
