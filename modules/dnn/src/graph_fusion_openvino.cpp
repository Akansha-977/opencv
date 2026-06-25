// This file is part of OpenCV project.
// It is subject to the license terms in the LICENSE file found in the top-level directory
// of this distribution and at http://opencv.org/license.html.

// Subgraph offload for the new graph engine: group maximal runs of OpenVINO-supported
// ops into one OpenVINOSubgraphLayer that compiles+runs them as a single ov::Model
// (so OpenVINO fuses across ops and data stays inside OpenVINO, like Engine Classic does).

#include "precomp.hpp"
#include "net_impl.hpp"
#include <opencv2/dnn/layer.details.hpp>

#ifdef HAVE_DNN_NGRAPH
#include "ie_ngraph.hpp"
#include "op_inf_engine.hpp"
#include <map>
#include <set>
#endif

namespace cv { namespace dnn {
CV__DNN_INLINE_NS_BEGIN

#ifdef HAVE_DNN_NGRAPH

using std::vector;

class OpenVINOSubgraphLayer : public Layer
{
public:
    vector<Ptr<Layer> > subops;

    OpenVINOSubgraphLayer() { built_ = false; }

    // Registered exec so finalizeGraph tags this op as a device op (backend=_NGRAPH) -> block layout
    // keeps its boundaries in standard (de-blocked) layout.  The op IS its own executor.
    static Ptr<Layer> create(const Ptr<OpData>& data, void*) { return data.dynamicCast<Layer>(); }

    virtual bool supportBackend(int backendId) CV_OVERRIDE
    {
        return backendId == DNN_BACKEND_INFERENCE_ENGINE_NGRAPH || backendId == DNN_BACKEND_OPENCV;
    }

    bool getMemoryShapes(const vector<MatShape>& inputs, const int,
                         vector<MatShape>& outputs, vector<MatShape>& internals) const CV_OVERRIDE
    {
        std::map<int, MatShape> shapeMap;
        for (size_t i = 0; i < this->inputs.size(); i++)
            shapeMap[this->inputs[i].idx] = inputs[i];
        for (const Ptr<Layer>& op : subops) {
            vector<MatShape> inShapes, outShapes, tmp;
            for (const Arg& a : op->inputs)
                inShapes.push_back(shapeOf(a, shapeMap));
            op->getMemoryShapes(inShapes, (int)op->outputs.size(), outShapes, tmp);
            for (size_t k = 0; k < op->outputs.size() && k < outShapes.size(); k++)
                shapeMap[op->outputs[k].idx] = outShapes[k];
        }
        outputs.clear();
        for (const Arg& a : this->outputs)
            outputs.push_back(shapeMap[a.idx]);
        internals.clear();
        return false;
    }

    void getTypes(const vector<MatType>& inputs, const int, const int,
                  vector<MatType>& outputs, vector<MatType>& internals) const CV_OVERRIDE
    {
        std::map<int, MatType> typeMap;
        for (size_t i = 0; i < this->inputs.size(); i++)
            typeMap[this->inputs[i].idx] = inputs[i];
        for (const Ptr<Layer>& op : subops) {
            vector<MatType> inTypes, outTypes, tmp;
            for (const Arg& a : op->inputs)
                inTypes.push_back(typeOf(a, typeMap));
            op->getTypes(inTypes, (int)op->outputs.size(), 0, outTypes, tmp);
            for (size_t k = 0; k < op->outputs.size() && k < outTypes.size(); k++)
                typeMap[op->outputs[k].idx] = outTypes[k];
        }
        outputs.clear();
        for (const Arg& a : this->outputs)
            outputs.push_back(typeMap[a.idx]);
        internals.clear();
    }

    int64_t getFLOPS(const vector<MatShape>& inputs, const vector<MatShape>&) const CV_OVERRIDE
    {
        std::map<int, MatShape> shapeMap;
        for (size_t i = 0; i < this->inputs.size(); i++)
            shapeMap[this->inputs[i].idx] = inputs[i];
        int64_t flops = 0;
        for (const Ptr<Layer>& op : subops) {
            vector<MatShape> inSh, outSh, tmpSh;
            for (const Arg& a : op->inputs) inSh.push_back(shapeOf(a, shapeMap));
            op->getMemoryShapes(inSh, (int)op->outputs.size(), outSh, tmpSh);
            for (size_t k = 0; k < op->outputs.size() && k < outSh.size(); k++)
                shapeMap[op->outputs[k].idx] = outSh[k];
            flops += op->getFLOPS(inSh, outSh);
        }
        return flops;
    }

    void forward(InputArrayOfArrays inputs_arr, OutputArrayOfArrays outputs_arr,
                 OutputArrayOfArrays internals_arr) CV_OVERRIDE
    {
        CV_UNUSED(internals_arr);
        vector<Mat> inpMats, outMats;
        inputs_arr.getMatVector(inpMats);
        outputs_arr.getMatVector(outMats);
        if (!built_)
            buildModel(inpMats);
        for (size_t i = 0; i < inpMats.size(); i++) {
            CV_Assert(inpMats[i].isContinuous());
            req_.set_input_tensor(i, ov::Tensor(cvTypeToOvType(inpMats[i]), ovShape(inpMats[i]), (void*)inpMats[i].data));
        }
        req_.infer();
        for (size_t i = 0; i < outMats.size(); i++) {
            Mat res = infEngineBlobToMat(req_.get_output_tensor(i));
            CV_CheckEQ(res.total() * res.elemSize(), outMats[i].total() * outMats[i].elemSize(),
                       "OpenVINO subgraph: output byte size mismatch");
            std::memcpy(outMats[i].data, res.data, res.total() * res.elemSize());
        }
    }

private:
    bool built_;
    ov::Core core_;
    ov::CompiledModel compiled_;
    ov::InferRequest req_;

    Net::Impl* ni() const { return static_cast<Net::Impl*>(netimpl); }  // OpData::netimpl is void*

    static ov::Shape ovShape(const Mat& m)
    {
        ov::Shape s;
        for (int i = 0; i < m.dims; i++) s.push_back((size_t)m.size[i]);
        return s;
    }
    MatShape shapeOf(const Arg& a, std::map<int, MatShape>& m) const
    {
        auto it = m.find(a.idx);
        return it != m.end() ? it->second : ni()->args[a.idx].shape;
    }
    MatType typeOf(const Arg& a, std::map<int, MatType>& m) const
    {
        auto it = m.find(a.idx);
        return it != m.end() ? it->second : ni()->args[a.idx].type;
    }

    void buildModel(const vector<Mat>& inpMats)
    {
        std::map<int, ov::Output<ov::Node> > nodeMap;
        std::map<int, MatShape> shapeMap;
        std::map<int, MatType> typeMap;
        ov::ParameterVector params;
        for (size_t i = 0; i < this->inputs.size(); i++) {
            auto p = std::make_shared<ov::op::v0::Parameter>(cvTypeToOvType(inpMats[i]), ovShape(inpMats[i]));
            nodeMap[this->inputs[i].idx] = p->output(0);
            shapeMap[this->inputs[i].idx] = inpMats[i].shape();
            typeMap[this->inputs[i].idx] = inpMats[i].type();
            params.push_back(p);
        }
        for (const Ptr<Layer>& op : subops) {
            // finalize each op (sets shape-derived members, e.g. global pool kernel/pads) before initNgraph
            vector<MatShape> inSh, outSh, tmpSh;
            vector<MatType> inTy, outTy, tmpTy;
            for (const Arg& a : op->inputs) { inSh.push_back(shapeOf(a, shapeMap)); inTy.push_back(typeOf(a, typeMap)); }
            op->getMemoryShapes(inSh, (int)op->outputs.size(), outSh, tmpSh);
            op->getTypes(inTy, (int)op->outputs.size(), (int)tmpSh.size(), outTy, tmpTy);
            std::vector<Mat> dIn(op->inputs.size()), dOut(op->outputs.size());
            for (size_t k = 0; k < inSh.size(); k++) dIn[k].fit(inSh[k], inTy[k]);
            for (size_t k = 0; k < outSh.size(); k++) dOut[k].fit(outSh[k], outTy[k]);
            op->finalize(dIn, dOut);
            for (size_t k = 0; k < op->outputs.size() && k < outSh.size(); k++) {
                shapeMap[op->outputs[k].idx] = outSh[k];
                typeMap[op->outputs[k].idx] = outTy[k];
            }

            vector<Ptr<BackendNode> > inputNodes;
            vector<Ptr<BackendWrapper> > inputWrappers(op->inputs.size());
            for (const Arg& a : op->inputs) {
                auto it = nodeMap.find(a.idx);
                if (it != nodeMap.end()) {
                    inputNodes.push_back(Ptr<BackendNode>(new InfEngineNgraphNode(it->second)));
                } else if (ni()->isConstArg(a)) {
                    Mat t = ni()->argTensor(a), t32;
                    if (t.type() == CV_32F) t32 = t; else t.convertTo(t32, CV_32F);
                    auto c = std::make_shared<ov::op::v0::Constant>(cvTypeToOvType(t32), ovShape(t32), t32.data);
                    nodeMap[a.idx] = c->output(0);
                    inputNodes.push_back(Ptr<BackendNode>(new InfEngineNgraphNode(c->output(0))));
                } else {
                    CV_Error(Error::StsError, "OpenVINO subgraph: unresolved input arg");
                }
            }
            Ptr<BackendNode> outNode = op->initNgraph(inputWrappers, inputNodes);
            Ptr<InfEngineNgraphNode> ieNode = outNode.dynamicCast<InfEngineNgraphNode>();
            CV_Assert(ieNode);
            nodeMap[op->outputs[0].idx] = ieNode->node;
            auto ovNode = ieNode->node.get_node_shared_ptr();
            for (size_t k = 1; k < op->outputs.size() && k < ovNode->get_output_size(); k++)
                nodeMap[op->outputs[k].idx] = ovNode->output(k);
        }
        ov::ResultVector results;
        for (const Arg& a : this->outputs) {
            auto it = nodeMap.find(a.idx);
            CV_Assert(it != nodeMap.end());
            results.push_back(std::make_shared<ov::op::v0::Result>(it->second));
        }
        auto model = std::make_shared<ov::Model>(results, params);
        compiled_ = core_.compile_model(model, "CPU");
        req_ = compiled_.create_infer_request();
        built_ = true;
    }
};

void registerOpenVINOSubgraphExec()
{
    CV_DNN_REGISTER_EXEC_CLASS(OpenVINOSubgraph, DNN_BACKEND_INFERENCE_ENGINE_NGRAPH, OpenVINOSubgraphLayer);
}

namespace {
struct ModelFusionOpenVINO
{
    Net::Impl* netimpl;
    int counter = 0;
    ModelFusionOpenVINO(Net::Impl* n) : netimpl(n) {}

    bool eligible(const Ptr<OpData>& op) const
    {
        if (!op || op->subgraphs())
            return false;
        Ptr<Layer> layer = op.dynamicCast<Layer>();
        return layer && layer->supportBackend(DNN_BACKEND_INFERENCE_ENGINE_NGRAPH);
    }

    void fuseGraph(Ptr<Graph>& graph)
    {
        const vector<Ptr<OpData> >& prog = graph->prog();
        size_t nops = prog.size();

        // recurse into control-flow subgraph bodies first
        for (const Ptr<OpData>& op : prog)
            if (op && op->subgraphs())
                for (Ptr<Graph>& sub : *op->subgraphs())
                    fuseGraph(sub);

        std::set<int> graphOutputs;
        for (const Arg& a : graph->outputs()) graphOutputs.insert(a.idx);
        std::map<int, int> consumerCount;
        for (const Ptr<OpData>& op : prog)
            if (op) for (const Arg& a : op->inputs) consumerCount[a.idx]++;

        vector<Ptr<OpData> > newprog;
        bool changed = false;
        size_t i = 0;
        while (i < nops) {
            if (!eligible(prog[i])) { newprog.push_back(prog[i]); i++; continue; }
            size_t j = i;
            std::set<int> produced;
            while (j < nops && eligible(prog[j])) {
                for (const Arg& a : prog[j]->outputs) produced.insert(a.idx);
                j++;
            }
            // group [i, j): external inputs / outputs
            std::map<int, int> insideConsumers;
            for (size_t k = i; k < j; k++)
                for (const Arg& a : prog[k]->inputs) insideConsumers[a.idx]++;

            vector<Arg> extIn, extOut;
            std::set<int> inSet, outSet;
            for (size_t k = i; k < j; k++)
                for (const Arg& a : prog[k]->inputs)
                    if (a.idx > 0 && !produced.count(a.idx) && !netimpl->isConstArg(a) && inSet.insert(a.idx).second)
                        extIn.push_back(a);
            for (size_t k = i; k < j; k++)
                for (const Arg& a : prog[k]->outputs) {
                    bool isOut = graphOutputs.count(a.idx) > 0;
                    int outside = consumerCount[a.idx] - insideConsumers[a.idx];
                    if ((isOut || outside > 0) && outSet.insert(a.idx).second)
                        extOut.push_back(a);
                }

            Ptr<OpenVINOSubgraphLayer> sg(new OpenVINOSubgraphLayer());
            sg->netimpl = netimpl;
            sg->type = "OpenVINOSubgraph";
            sg->name = cv::format("openvino_subgraph_%d", counter++);
            sg->inputs = extIn;
            sg->outputs = extOut;
            for (size_t k = i; k < j; k++)
                sg->subops.push_back(prog[k].dynamicCast<Layer>());
            newprog.push_back(sg);
            changed = true;
            i = j;
        }
        if (changed)
            graph->setProg(newprog);
    }
};
} // namespace

void Net::Impl::fuseOpenVINO()
{
    if (!mainGraph)
        return;
    ModelFusionOpenVINO pass(this);
    pass.fuseGraph(mainGraph);
}

#else  // !HAVE_DNN_NGRAPH

void registerOpenVINOSubgraphExec() {}
void Net::Impl::fuseOpenVINO() {}

#endif  // HAVE_DNN_NGRAPH

CV__DNN_INLINE_NS_END
}}  // namespace cv::dnn
