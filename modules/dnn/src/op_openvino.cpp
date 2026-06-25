// This file is part of OpenCV project.
// It is subject to the license terms in the LICENSE file found in the top-level directory
// of this distribution and at http://opencv.org/license.html.

#include "precomp.hpp"
#include "net_impl.hpp"
#include <opencv2/dnn/layer.details.hpp>

#ifdef HAVE_DNN_NGRAPH
#include "ie_ngraph.hpp"
#include "op_inf_engine.hpp"
#endif

namespace cv { namespace dnn {
CV__DNN_INLINE_NS_BEGIN

#ifdef HAVE_DNN_NGRAPH

// Per-op OpenVINO executor for the new graph engine: wraps the monolithic CPU layer and reuses
// its existing initNgraph() to build a single-op ov::Model, then runs it on the CPU plugin
// directly over the host Mats (no device copy).
class OpenVINOLegacyExec : public Layer
{
public:
    OpenVINOLegacyExec(const Ptr<Layer>& impl_) : impl(impl_) {}

    static Ptr<Layer> create(const Ptr<OpData>& data, void* /*backendCtx*/)
    {
        Ptr<Layer> impl = data.dynamicCast<Layer>();
        if (!impl || !impl->supportBackend(DNN_BACKEND_INFERENCE_ENGINE_NGRAPH))
            return Ptr<Layer>();  // unsupported -> CPU fallback
        Ptr<OpenVINOLegacyExec> e(new OpenVINOLegacyExec(impl));
        e->data = data;
        e->name = impl->name;
        e->type = impl->type;
        e->inputs = impl->inputs;
        e->outputs = impl->outputs;
        return e;
    }

    void finalize(InputArrayOfArrays inputs, OutputArrayOfArrays outputs) CV_OVERRIDE
    {
        impl->finalize(inputs, outputs);
    }

    void forward(InputArrayOfArrays inputs_arr,
                 OutputArrayOfArrays outputs_arr,
                 OutputArrayOfArrays internals_arr) CV_OVERRIDE
    {
        CV_UNUSED(internals_arr);
        std::vector<Mat> inpMats, outMats;
        inputs_arr.getMatVector(inpMats);
        outputs_arr.getMatVector(outMats);

        if (!compiledModel)
            buildModel(inpMats);

        for (size_t i = 0; i < inpMats.size(); i++) {
            const Mat& m = inpMats[i];
            CV_Assert(m.isContinuous());
            req.set_input_tensor(i, ov::Tensor(cvTypeToOvType(m), ovShape(m), (void*)m.data));
        }
        req.infer();
        for (size_t i = 0; i < outMats.size(); i++) {
            Mat res = infEngineBlobToMat(req.get_output_tensor(i));
            CV_CheckEQ(res.total() * res.elemSize(), outMats[i].total() * outMats[i].elemSize(),
                       "OpenVINO exec: output byte size mismatch");
            std::memcpy(outMats[i].data, res.data, res.total() * res.elemSize());
        }
    }

private:
    static ov::Shape ovShape(const Mat& m)
    {
        ov::Shape s;
        for (int i = 0; i < m.dims; i++)
            s.push_back((size_t)m.size[i]);
        return s;
    }

    void buildModel(const std::vector<Mat>& inpMats)
    {
        ov::ParameterVector params;
        std::vector<Ptr<BackendNode> > inputNodes;
        std::vector<Ptr<BackendWrapper> > inputWrappers;
        for (size_t i = 0; i < inpMats.size(); i++) {
            const Mat& m = inpMats[i];
            auto param = std::make_shared<ov::op::v0::Parameter>(cvTypeToOvType(m), ovShape(m));
            params.push_back(param);
            inputNodes.push_back(Ptr<BackendNode>(new InfEngineNgraphNode(param->output(0))));
            inputWrappers.push_back(Ptr<BackendWrapper>(new NgraphBackendWrapper(DNN_TARGET_CPU, m)));
        }

        Ptr<BackendNode> outNode = impl->initNgraph(inputWrappers, inputNodes);
        Ptr<InfEngineNgraphNode> ieNode = outNode.dynamicCast<InfEngineNgraphNode>();
        CV_Assert(ieNode);

        auto result = std::make_shared<ov::op::v0::Result>(ieNode->node);
        auto model = std::make_shared<ov::Model>(ov::ResultVector{result}, params);

        compiledModel = std::make_shared<ov::CompiledModel>(core.compile_model(model, "CPU"));
        req = compiledModel->create_infer_request();
    }

    Ptr<Layer> impl;
    ov::Core core;
    std::shared_ptr<ov::CompiledModel> compiledModel;
    ov::InferRequest req;
};

void registerOpenVINOCommonExecs()
{
    CV_DNN_REGISTER_EXEC_CLASS(Conv2,       DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(ReLU,        DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(ReLU6,       DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(NaryEltwise, DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(Flatten,     DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(BatchNorm2,  DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(MaxPool,     DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(Gemm,        DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
    CV_DNN_REGISTER_EXEC_CLASS(Pooling,     DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOLegacyExec);
}

#else

void registerOpenVINOCommonExecs() {}

#endif  // HAVE_DNN_NGRAPH

CV__DNN_INLINE_NS_END
}}  // namespace cv::dnn
