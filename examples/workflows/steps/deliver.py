"""Declare deliverables=[delivery] in a final step."""
arcpy.management.CopyFeatures(inputs['features'], outputs['delivery'])
